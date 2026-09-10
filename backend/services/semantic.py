from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from ..core.database import Database, utcnow
from .authorization import require_source_access
from .datasets import execute_query, schema_for_source


NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .-]{0,127}$")
FIELD_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]{0,127}$")
AGGREGATIONS = {"sum", "avg", "min", "max", "count", "count_distinct"}
ENTITY_TYPES = {"primary", "foreign", "unique", "natural"}
DIMENSION_TYPES = {"categorical", "time", "boolean", "numeric"}
METRIC_STATUSES = {"draft", "approved", "deprecated"}
METRIC_TYPES = {"atomic", "derived", "composite"}
TIME_GRAINS = {"day", "week", "month", "quarter", "year"}
FILTER_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "in", "between", "is_null", "is_not_null"}


def _fingerprint(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _store_version(database: Database, collection: str, record: dict) -> None:
    database.put(
        collection,
        {
            **record, "id": f"{record['id']}:{record['version']}",
            "definition_id": record["id"], "snapshot_at": utcnow(),
        },
        workspace_id=record["workspace_id"],
    )


def _name(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not NAME_RE.fullmatch(normalized):
        raise ValueError(f"{label}格式无效")
    return normalized


def _field(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not FIELD_RE.fullmatch(normalized):
        raise ValueError(f"{label}格式无效")
    return normalized


def _table(source: dict, requested: str) -> dict:
    return next(
        (
            item for item in source.get("tables") or []
            if requested in {str(item.get("name") or ""), str(item.get("source_name") or "")}
        ),
        None,
    ) or (_ for _ in ()).throw(ValueError(f"语义模型数据表不存在：{requested}"))


def _columns(table: dict) -> set[str]:
    schema = table.get("schema") or []
    if schema and isinstance(schema[0], dict):
        return {str(item.get("name") or "") for item in schema}
    columns = table.get("column_names") or []
    return {str(value) for value in columns}


def _normalize_items(
    raw: Any, *, kind: str, columns: set[str], required: bool = False,
) -> list[dict]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 500:
        raise ValueError(f"语义模型 {kind} 必须是不超过 500 项的数组")
    if required and not raw:
        raise ValueError(f"语义模型至少需要一个 {kind}")
    output: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"语义模型 {kind} 项必须是对象")
        name = _field(item.get("name"), f"{kind} 名称")
        if name in seen:
            raise ValueError(f"语义字段名重复：{name}")
        seen.add(name)
        aggregation = str(item.get("aggregation") or "sum").lower() if kind == "measures" else ""
        raw_column = item.get("column") or name
        column = "*" if kind == "measures" and aggregation == "count" and raw_column == "*" else _field(raw_column, f"{kind} 字段")
        if columns and column != "*" and column not in columns:
            raise ValueError(f"语义字段不在数据表中：{column}")
        value = {
            "name": name, "column": column,
            "label": str(item.get("label") or name)[:160],
            "description": str(item.get("description") or "")[:2000],
        }
        if kind == "entities":
            value["type"] = str(item.get("type") or "foreign").lower()
            if value["type"] not in ENTITY_TYPES:
                raise ValueError(f"实体类型无效：{value['type']}")
        elif kind == "dimensions":
            value["type"] = str(item.get("type") or "categorical").lower()
            if value["type"] not in DIMENSION_TYPES:
                raise ValueError(f"维度类型无效：{value['type']}")
        else:
            value["aggregation"] = aggregation
            if value["aggregation"] not in AGGREGATIONS:
                raise ValueError(f"度量聚合方式无效：{value['aggregation']}")
        output.append(value)
    return output


def save_model(
    database: Database, payload: dict, workspace_id: str, actor_id: str,
    model_id: str | None = None,
) -> dict:
    current = database.get("semantic_models", model_id, workspace_id=workspace_id) if model_id else None
    source_id = str(payload.get("source_id") or (current or {}).get("source_id") or "")
    source = require_source_access(
        database, source_id, workspace_id=workspace_id, actor_id=actor_id, action="update",
    )
    table_name = str(payload.get("table") or (current or {}).get("table") or "").strip()
    table = _table(source, table_name)
    columns = _columns(table)
    if not columns:
        detailed = next(
            (
                item for item in schema_for_source(source).get("tables") or []
                if table_name in {str(item.get("name") or ""), str(item.get("source_name") or "")}
            ),
            None,
        )
        if detailed:
            columns = {str(item.get("name") or "") for item in detailed.get("columns") or []}
    merged = {**(current or {}), **payload}
    name = _name(merged.get("name"), "语义模型名称")
    entities = _normalize_items(merged.get("entities"), kind="entities", columns=columns)
    dimensions = _normalize_items(merged.get("dimensions"), kind="dimensions", columns=columns)
    measures = _normalize_items(merged.get("measures"), kind="measures", columns=columns, required=True)
    default_time_dimension = str(merged.get("default_time_dimension") or "").strip()
    if default_time_dimension:
        time_dimensions = {item["name"] for item in dimensions if item["type"] == "time"}
        if default_time_dimension not in time_dimensions:
            raise ValueError("默认时间维度必须引用 time 类型维度")
    identifier = model_id or database.new_id("sem")
    version = int((current or {}).get("version") or 0) + 1
    record = {
        "id": identifier, "workspace_id": workspace_id, "source_id": source_id,
        "name": name, "description": str(merged.get("description") or "")[:4000],
        "table": str(table.get("name") or table_name),
        "source_table": str(table.get("source_name") or table_name),
        "schema_name": str(table.get("schema_name") or source.get("schema_name") or ""),
        "grain": str(merged.get("grain") or "")[:500], "entities": entities,
        "dimensions": dimensions, "measures": measures,
        "default_time_dimension": default_time_dimension,
        "version": version, "enabled": bool(merged.get("enabled", True)),
        "created_by": (current or {}).get("created_by") or actor_id,
        "created_at": (current or {}).get("created_at") or utcnow(),
        "updated_by": actor_id, "updated_at": utcnow(),
    }
    record["definition_fingerprint"] = _fingerprint({
        key: record.get(key) for key in (
            "source_id", "table", "source_table", "schema_name", "grain",
            "entities", "dimensions", "measures", "default_time_dimension", "enabled",
        )
    })
    stored = database.put("semantic_models", record, workspace_id=workspace_id)
    _store_version(database, "semantic_model_versions", stored)
    invalidated = []
    if current and current.get("definition_fingerprint") != stored["definition_fingerprint"]:
        for metric in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000):
            if metric.get("model_id") != identifier or metric.get("status") != "approved":
                continue
            changed = {
                **metric, "status": "draft", "version": int(metric.get("version") or 0) + 1,
                "approval_invalidated_by_model_version": version,
                "updated_by": actor_id, "updated_at": utcnow(),
            }
            changed["definition_fingerprint"] = _fingerprint({
                key: changed.get(key) for key in (
                    "model_id", "measure", "filters", "unit", "format", "status",
                )
            })
            changed = database.put("semantic_metrics", changed, workspace_id=workspace_id)
            _store_version(database, "semantic_metric_versions", changed)
            invalidated.append(metric["id"])
    database.audit(
        "semantic.model_saved", workspace_id=workspace_id, actor=actor_id,
        object_type="semantic_model", object_id=identifier,
        detail={
            "version": version, "source_id": source_id, "table": record["table"],
            "definition_fingerprint": record["definition_fingerprint"],
            "invalidated_metric_ids": invalidated,
        },
    )
    return stored


def _formula_references(expression: str) -> tuple[exp.Expression, list[str]]:
    value = str(expression or "").strip()
    if not value or len(value) > 2000:
        raise ValueError("派生/复合指标必须提供不超过 2000 字符的计算公式")
    try:
        tree = sqlglot.parse_one(value, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise ValueError("指标计算公式语法无效") from exc
    allowed = (
        exp.Column, exp.Identifier, exp.Literal, exp.Add, exp.Sub, exp.Mul,
        exp.Div, exp.Mod, exp.Neg, exp.Paren,
    )
    if any(not isinstance(node, allowed) for node in tree.walk()):
        raise ValueError("指标公式只允许指标名称、数字和 + - * / % 括号")
    references = list(dict.fromkeys(str(node.name) for node in tree.find_all(exp.Column)))
    if not references:
        raise ValueError("派生/复合指标公式至少需要引用一个已有指标")
    return tree, references


def save_metric(
    database: Database, payload: dict, workspace_id: str, actor_id: str,
    metric_id: str | None = None,
) -> dict:
    current = database.get("semantic_metrics", metric_id, workspace_id=workspace_id) if metric_id else None
    merged = {**(current or {}), **payload}
    model_id = str(merged.get("model_id") or "")
    model = database.get("semantic_models", model_id, workspace_id=workspace_id)
    if not model or not model.get("enabled", True):
        raise ValueError("语义指标必须引用已启用的语义模型")
    require_source_access(
        database, model["source_id"], workspace_id=workspace_id, actor_id=actor_id, action="update",
    )
    metric_type = str(merged.get("metric_type") or "atomic").lower()
    if metric_type not in METRIC_TYPES:
        raise ValueError("指标类型必须是 atomic、derived 或 composite")
    measure = str(merged.get("measure") or "").strip()
    expression = str(merged.get("expression") or "").strip()
    dependencies: list[str] = []
    if metric_type == "atomic":
        measure = _field(measure, "指标度量")
        if measure not in {item["name"] for item in model.get("measures") or []}:
            raise ValueError(f"指标引用的度量不存在：{measure}")
        expression = ""
    else:
        _tree, references = _formula_references(expression)
        candidates = {
            str(item.get("name")): item
            for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
            if item.get("model_id") == model_id and item.get("id") != metric_id
        }
        missing = [name for name in references if name not in candidates]
        if missing:
            raise ValueError(f"指标公式引用了不存在或不同模型的指标：{', '.join(missing)}")
        dependencies = [candidates[name]["id"] for name in references]
        measure = ""
    status = str(merged.get("status") or "draft").lower()
    if status not in METRIC_STATUSES:
        raise ValueError("指标状态必须是 draft、approved 或 deprecated")
    if status == "approved" and metric_type != "atomic":
        unresolved = [
            item_id for item_id in dependencies
            if (database.get("semantic_metrics", item_id, workspace_id=workspace_id) or {}).get("status") != "approved"
        ]
        if unresolved:
            raise ValueError("派生/复合指标只有在全部依赖指标已审批后才能发布")
    aliases = merged.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [value.strip() for value in aliases.split(",") if value.strip()]
    if not isinstance(aliases, list) or len(aliases) > 100:
        raise ValueError("指标别名必须是不超过 100 项的数组")
    filters = merged.get("filters") or []
    if not isinstance(filters, list) or len(filters) > 100:
        raise ValueError("指标固定过滤条件必须是不超过 100 项的数组")
    dimensions = {item["name"]: item for item in model.get("dimensions") or []}
    for value in filters:
        _filter_sql(value, dimensions)
    identifier = metric_id or database.new_id("metric")
    version = int((current or {}).get("version") or 0) + 1
    name = _field(merged.get("name"), "指标名称")
    label = str(merged.get("label") or name)[:160]
    lookup_names = {name.lower(), label.strip().lower(), *(str(value).strip().lower() for value in aliases)}
    for other in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000):
        if other["id"] == identifier:
            continue
        other_names = {
            str(other.get("name") or "").lower(), str(other.get("label") or "").strip().lower(),
            *(str(value).strip().lower() for value in other.get("aliases") or []),
        }
        if lookup_names & other_names:
            raise ValueError("指标名称、显示名称或别名与已有指标冲突")
    record = {
        "id": identifier, "workspace_id": workspace_id,
        "name": name,
        "label": label,
        "description": str(merged.get("description") or "")[:4000],
        "model_id": model_id, "metric_type": metric_type, "measure": measure,
        "expression": expression, "dependency_metric_ids": dependencies, "status": status,
        "aliases": list(dict.fromkeys(str(value).strip()[:160] for value in aliases if str(value).strip())),
        "filters": filters,
        "unit": str(merged.get("unit") or "")[:50],
        "format": str(merged.get("format") or "")[:100],
        "business_object": str(merged.get("business_object") or "")[:200],
        "business_event": str(merged.get("business_event") or "")[:200],
        "grain": str(merged.get("grain") or model.get("grain") or "")[:500],
        "time_semantics": str(merged.get("time_semantics") or "")[:500],
        "deduplication": str(merged.get("deduplication") or "")[:500],
        "business_owner": str(merged.get("business_owner") or "")[:160],
        "technical_owner": str(merged.get("technical_owner") or "")[:160],
        "certification_note": str(merged.get("certification_note") or "")[:2000],
        "created_by": (current or {}).get("created_by") or actor_id,
        "created_at": (current or {}).get("created_at") or utcnow(),
        "version": version, "updated_by": actor_id, "updated_at": utcnow(),
    }
    if status == "approved":
        record["approved_by"] = actor_id
        record["approved_at"] = utcnow()
    else:
        record["approved_by"] = None
        record["approved_at"] = None
    record["definition_fingerprint"] = _fingerprint({
        key: record.get(key) for key in (
            "model_id", "metric_type", "measure", "expression", "dependency_metric_ids",
            "filters", "unit", "format", "business_object", "business_event", "grain",
            "time_semantics", "deduplication", "status",
        )
    })
    stored = database.put("semantic_metrics", record, workspace_id=workspace_id)
    _store_version(database, "semantic_metric_versions", stored)
    database.audit(
        "semantic.metric_saved", workspace_id=workspace_id, actor=actor_id,
        object_type="semantic_metric", object_id=identifier,
        detail={
            "version": version, "status": status, "model_id": model_id,
            "definition_fingerprint": record["definition_fingerprint"],
        },
    )
    return stored


def visible_metrics(database: Database, workspace_id: str, actor_id: str) -> list[dict]:
    output = []
    for metric in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000):
        model = database.get("semantic_models", str(metric.get("model_id") or ""), workspace_id=workspace_id)
        if not model or not model.get("enabled", True):
            continue
        try:
            require_source_access(
                database, model["source_id"], workspace_id=workspace_id, actor_id=actor_id,
            )
        except (FileNotFoundError, PermissionError):
            continue
        output.append({**metric, "source_id": model["source_id"], "model_name": model["name"]})
    return output


def _resolve_metric(database: Database, workspace_id: str, actor_id: str, value: str) -> tuple[dict, dict]:
    target = str(value or "").strip().lower()
    candidates = []
    for metric in visible_metrics(database, workspace_id, actor_id):
        names = {str(metric.get("id") or ""), str(metric.get("name") or ""), str(metric.get("label") or "")}
        names.update(str(alias) for alias in metric.get("aliases") or [])
        if target in {name.strip().lower() for name in names if name.strip()}:
            candidates.append(metric)
    if not candidates:
        raise ValueError(f"未找到可访问的语义指标：{value}")
    if len(candidates) > 1:
        raise ValueError(f"指标名称存在歧义，请使用指标 ID：{value}")
    metric = candidates[0]
    if metric.get("status") != "approved":
        raise PermissionError("仅已审批指标可用于正式问数")
    model = database.get("semantic_models", metric["model_id"], workspace_id=workspace_id)
    return metric, model


def _literal(value: Any) -> str:
    return exp.convert(value).sql(dialect="duckdb")


def _column(value: str) -> str:
    return exp.column(value, quoted=True).sql(dialect="duckdb")


def _filter_sql(raw: dict, dimensions: dict[str, dict]) -> str:
    if not isinstance(raw, dict):
        raise ValueError("过滤条件必须是对象")
    dimension_name = _field(raw.get("dimension") or raw.get("field"), "过滤维度")
    dimension = dimensions.get(dimension_name)
    if not dimension:
        raise ValueError(f"过滤条件引用了未定义维度：{dimension_name}")
    operator = str(raw.get("op") or "=").lower()
    if operator not in FILTER_OPERATORS:
        raise ValueError(f"过滤操作符无效：{operator}")
    column = _column(dimension["column"])
    value = raw.get("value")
    if operator == "in":
        if not isinstance(value, list) or not value or len(value) > 100:
            raise ValueError("in 过滤需要 1-100 个值")
        return f"{column} IN ({', '.join(_literal(item) for item in value)})"
    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("between 过滤需要两个边界值")
        return f"{column} BETWEEN {_literal(value[0])} AND {_literal(value[1])}"
    if operator == "is_null":
        return f"{column} IS NULL"
    if operator == "is_not_null":
        return f"{column} IS NOT NULL"
    return f"{column} {operator.upper()} {_literal(value)}"


def _dialect(source: dict) -> str:
    if source.get("kind") != "database":
        return "duckdb"
    driver = str(source.get("driver") or "").lower()
    if "postgres" in driver:
        return "postgres"
    if "mysql" in driver:
        return "mysql"
    if "mssql" in driver or "sqlserver" in driver:
        return "tsql"
    return "sqlite" if "sqlite" in driver else driver


def _atomic_metric_expression(metric: dict, measures: dict[str, dict], dimensions: dict[str, dict]) -> str:
    measure = measures.get(str(metric.get("measure") or ""))
    if not measure:
        raise ValueError(f"指标引用的度量不存在：{metric.get('measure')}")
    aggregation = measure["aggregation"]
    column = "*" if measure["column"] == "*" else _column(measure["column"])
    fixed = [_filter_sql(item, dimensions) for item in metric.get("filters") or []]
    predicate = " AND ".join(f"({item})" for item in fixed)
    if predicate:
        if aggregation == "count_distinct":
            return f"COUNT(DISTINCT CASE WHEN {predicate} THEN {column} END)"
        if aggregation == "count":
            return f"COUNT(CASE WHEN {predicate} THEN 1 END)"
        return f"{aggregation.upper()}(CASE WHEN {predicate} THEN {column} END)"
    if aggregation == "count_distinct":
        return f"COUNT(DISTINCT {column})"
    if aggregation == "count":
        return f"COUNT({column})"
    return f"{aggregation.upper()}({column})"


def _compiled_metric_expression(
    metric: dict, metrics: dict[str, dict], measures: dict[str, dict],
    dimensions: dict[str, dict], stack: tuple[str, ...] = (),
) -> str:
    name = str(metric.get("name") or "")
    if name in stack:
        raise ValueError("指标公式存在循环依赖")
    if str(metric.get("metric_type") or "atomic") == "atomic":
        return _atomic_metric_expression(metric, measures, dimensions)
    tree, references = _formula_references(str(metric.get("expression") or ""))
    for reference in references:
        dependency = metrics.get(reference)
        if not dependency or dependency.get("status") != "approved":
            raise ValueError(f"指标公式依赖未审批或不存在：{reference}")

    def replace(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            dependency_sql = _compiled_metric_expression(
                metrics[node.name], metrics, measures, dimensions, (*stack, name),
            )
            return exp.Paren(this=sqlglot.parse_one(dependency_sql, read="duckdb"))
        return node

    return tree.transform(replace).sql(dialect="duckdb")


def compile_metric_query(
    database: Database, request: dict, workspace_id: str, actor_id: str,
) -> dict:
    metric, model = _resolve_metric(
        database, workspace_id, actor_id, str(request.get("metric") or request.get("metric_id") or ""),
    )
    source = require_source_access(
        database, model["source_id"], workspace_id=workspace_id, actor_id=actor_id, action="query",
    )
    dimensions = {item["name"]: item for item in model.get("dimensions") or []}
    measures = {item["name"]: item for item in model.get("measures") or []}
    model_metrics = {
        str(item.get("name")): item
        for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
        if item.get("model_id") == model["id"]
    }
    raw_group = request.get("group_by") or []
    if not isinstance(raw_group, list) or len(raw_group) > 20:
        raise ValueError("group_by 必须是不超过 20 项的数组")
    group_expressions: list[tuple[str, str]] = []
    for item in raw_group:
        config = {"dimension": item} if isinstance(item, str) else item
        if not isinstance(config, dict):
            raise ValueError("group_by 项必须是维度名称或对象")
        name = _field(config.get("dimension") or config.get("name"), "分组维度")
        dimension = dimensions.get(name)
        if not dimension:
            raise ValueError(f"指标不支持分组维度：{name}")
        expression = _column(dimension["column"])
        grain = str(config.get("grain") or "").lower()
        if grain:
            if dimension.get("type") != "time" or grain not in TIME_GRAINS:
                raise ValueError(f"时间粒度无效：{grain}")
            expression = f"DATE_TRUNC('{grain}', {expression})"
        group_expressions.append((name, expression))
    if metric["name"] in {name for name, _expression in group_expressions}:
        raise ValueError("指标技术名称不能与分组维度同名")
    metric_expression = _compiled_metric_expression(metric, model_metrics, measures, dimensions)
    selections = [f"{expression} AS {_column(name)}" for name, expression in group_expressions]
    selections.append(f"{metric_expression} AS {_column(metric['name'])}")
    table_name = str(model.get("source_table") or model["table"])
    schema_name = str(model.get("schema_name") or "")
    table_sql = _column(table_name)
    if schema_name:
        table_sql = f"{_column(schema_name)}.{table_sql}"
    formula_filters = list(metric.get("filters") or []) if metric.get("metric_type") in {"derived", "composite"} else []
    filters = formula_filters + list(request.get("filters") or [])
    if len(filters) > 100:
        raise ValueError("指标过滤条件超过 100 项上限")
    predicates = [_filter_sql(item, dimensions) for item in filters]
    time_range = request.get("time_range") or {}
    if time_range:
        if not isinstance(time_range, dict):
            raise ValueError("time_range 必须是对象")
        time_name = str(time_range.get("dimension") or model.get("default_time_dimension") or "")
        time_dimension = dimensions.get(time_name)
        if not time_dimension or time_dimension.get("type") != "time":
            raise ValueError("指标未定义可用的时间维度")
        if time_range.get("start") is not None:
            predicates.append(f"{_column(time_dimension['column'])} >= {_literal(time_range['start'])}")
        if time_range.get("end") is not None:
            predicates.append(f"{_column(time_dimension['column'])} < {_literal(time_range['end'])}")
    # Every identifier is schema-validated and quoted; every value is emitted
    # by sqlglot's literal serializer above.
    sql = f"SELECT {', '.join(selections)} FROM {table_sql}"  # noqa: S608
    if predicates:
        sql += " WHERE " + " AND ".join(f"({value})" for value in predicates)
    if group_expressions:
        sql += " GROUP BY " + ", ".join(expression for _name, expression in group_expressions)
    order_by = request.get("order_by") or []
    if isinstance(order_by, dict):
        order_by = [order_by]
    if order_by:
        if not isinstance(order_by, list) or len(order_by) > 10:
            raise ValueError("order_by 必须是不超过 10 项的数组")
        allowed_order = {metric["name"], *(name for name, _value in group_expressions)}
        ordering = []
        for item in order_by:
            item = {"field": item} if isinstance(item, str) else item
            field = _field(item.get("field"), "排序字段")
            if field not in allowed_order:
                raise ValueError(f"排序字段不在指标输出中：{field}")
            direction = str(item.get("direction") or "asc").lower()
            if direction not in {"asc", "desc"}:
                raise ValueError("排序方向必须是 asc 或 desc")
            ordering.append(f"{_column(field)} {direction.upper()}")
        sql += " ORDER BY " + ", ".join(ordering)
    limit = max(1, min(int(request.get("limit") or 1000), 5000))
    sql += f" LIMIT {limit}"
    dialect = _dialect(source)
    rendered = sqlglot.transpile(sql, read="duckdb", write=dialect)[0]
    return {
        "metric": {
            "id": metric["id"], "name": metric["name"], "label": metric["label"],
            "version": metric["version"], "unit": metric.get("unit", ""),
            "metric_type": metric.get("metric_type", "atomic"),
            "expression": metric.get("expression", ""),
            "dependency_metric_ids": metric.get("dependency_metric_ids") or [],
            "definition_fingerprint": metric.get("definition_fingerprint"),
        },
        "model": {
            "id": model["id"], "name": model["name"], "version": model["version"],
            "definition_fingerprint": model.get("definition_fingerprint"),
        },
        "source_id": source["id"], "dialect": dialect, "sql": rendered,
        "group_by": [name for name, _value in group_expressions],
        "filters": filters, "certified_filters": metric.get("filters") or [],
        "time_range": time_range, "limit": limit,
    }


def execute_metric_query(
    database: Database, request: dict, workspace_id: str, actor_id: str, *,
    allowed_source_ids: list[str] | None = None,
) -> dict:
    plan = compile_metric_query(database, request, workspace_id, actor_id)
    if allowed_source_ids is not None and plan["source_id"] not in {str(value) for value in allowed_source_ids}:
        raise PermissionError("指标不属于任务已确认的数据源范围")
    result = execute_query(
        [plan["source_id"]], plan["sql"], workspace_id, plan["limit"], actor_id=actor_id,
    )
    semantic = {
        "metric_id": plan["metric"]["id"], "metric_name": plan["metric"]["name"],
        "metric_version": plan["metric"]["version"], "model_id": plan["model"]["id"],
        "model_version": plan["model"]["version"], "group_by": plan["group_by"],
        "metric_definition_fingerprint": plan["metric"]["definition_fingerprint"],
        "model_definition_fingerprint": plan["model"]["definition_fingerprint"],
        "filters": plan["filters"], "time_range": plan["time_range"],
    }
    result = database.patch(
        "query_results", result["id"], {"semantic_query": semantic}, workspace_id=workspace_id,
    ) or result
    database.audit(
        "semantic.metric_queried", workspace_id=workspace_id, actor=actor_id,
        object_type="semantic_metric", object_id=plan["metric"]["id"],
        detail={"query_id": result["id"], "metric_version": plan["metric"]["version"]},
    )
    return {"plan": plan, "result": result}
