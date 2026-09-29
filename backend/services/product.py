from __future__ import annotations

from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage

from ..core.database import Database

SAMPLE_SEED_ID = "instant_retail_city_pack"
DEPLOYMENT_FEATURES = frozenset({
    "data_sources", "governed_agent", "knowledge_base", "semantic_layer",
    "result_delivery", "mcp_integrations", "warehouse", "workspace_governance",
    "audit", "lifecycle_management",
})


def workspace_entitlements(database: Database, workspace_id: str) -> dict[str, Any]:
    """Fixed capabilities of this deployment, independent of billing records."""
    return {"workspace_id": workspace_id, "features": sorted(DEPLOYMENT_FEATURES)}


def product_status(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    return {
        "entitlements": workspace_entitlements(database, workspace_id),
        "onboarding": onboarding_status(database, workspace_id),
    }


def assert_feature_enabled(database: Database, workspace_id: str, feature: str) -> dict[str, Any]:
    if feature not in DEPLOYMENT_FEATURES:
        raise PermissionError(f"当前部署不支持功能：{feature}")
    return workspace_entitlements(database, workspace_id)


def onboarding_status(database: Database, workspace_id: str) -> dict[str, Any]:
    source_count = len(database.list("sources", workspace_id=workspace_id, limit=5000))
    knowledge_count = len(database.list("knowledge_documents", workspace_id=workspace_id, limit=5000)) + len(
        database.list("knowledge_entries", workspace_id=workspace_id, limit=5000),
    )
    metric_count = len([
        item for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
        if item.get("status") == "approved"
    ])
    run_count = len(database.list("publications", workspace_id=workspace_id, limit=5000)) + len([
        item for item in database.list("agent_runs", workspace_id=workspace_id, limit=5000)
        if item.get("execution_status") == "finished"
    ])
    steps = [
        {
            "id": "connect_data", "name": "接入数据",
            "description": "至少登记一个可预览、可查询的数据源。",
            "done": source_count > 0, "count": source_count, "route": "sources",
        },
        {
            "id": "define_context", "name": "沉淀业务口径",
            "description": "录入指标定义、业务规则或知识文档，回答才有业务语境。",
            "done": knowledge_count > 0, "count": knowledge_count, "route": "knowledge",
        },
        {
            "id": "approve_metrics", "name": "审批核心指标",
            "description": "建立语义模型并审批至少一个正式指标。",
            "done": metric_count > 0, "count": metric_count, "route": "semantic",
        },
        {
            "id": "run_analysis", "name": "完成一次受治理分析",
            "description": "确认需求理解后执行 Agent，并生成可追溯结果。",
            "done": run_count > 0, "count": run_count, "route": "chat",
        },
    ]
    required_done = all(item["done"] for item in steps[:4])
    return {
        "workspace_id": workspace_id,
        "complete": required_done,
        "score": round(sum(1 for item in steps if item["done"]) / len(steps), 4),
        "steps": steps,
        "next_step": next((item for item in steps if not item["done"]), None),
        "demo_available": True,
        "sample_seed_id": SAMPLE_SEED_ID,
    }



def seed_demo_workspace(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    source = _existing_sample_source(database, workspace_id)
    created: list[str] = []
    if source is None:
        source = _register_sample_source(workspace_id)
        patched = database.patch(
            "sources",
            source["id"],
            {
                "name": "即时零售 10 城经营样例",
                "description": "内置标准演示数据：城市、订单、市占率、客单价、履约成本、补贴、用户与商家结构。",
                "classification": "internal",
                "sensitivity": "internal",
                "sample_seed": {"id": SAMPLE_SEED_ID, "version": 1},
            },
            workspace_id=workspace_id,
        )
        source = patched or source
        created.append("source")

    entries_created = _ensure_sample_knowledge(database, workspace_id)
    created.extend(["knowledge_entry"] * entries_created)
    semantic_created = _ensure_sample_semantic(database, workspace_id, source, actor_id)
    created.extend(semantic_created)
    _attach_sample_to_active_session(database, workspace_id, source["id"])
    database.audit(
        "product.demo_seeded", workspace_id=workspace_id, actor=actor_id,
        object_type="sample_seed", object_id=SAMPLE_SEED_ID,
        detail={"created": created, "source_id": source["id"]},
    )
    return {
        "created": created,
        "source": source,
        "onboarding": onboarding_status(database, workspace_id),
        "entitlements": workspace_entitlements(database, workspace_id),
    }


def _sample_path() -> Path:
    return Path(__file__).resolve().parents[2] / "deploy" / "samples" / "Sample-data.xlsx"


def _existing_sample_source(database: Database, workspace_id: str) -> dict[str, Any] | None:
    for source in database.list("sources", workspace_id=workspace_id, limit=5000):
        seed = source.get("sample_seed") or {}
        if seed.get("id") == SAMPLE_SEED_ID:
            return source
    return None


def _register_sample_source(workspace_id: str) -> dict[str, Any]:
    from .datasets import register_upload

    path = _sample_path()
    if not path.is_file():
        raise FileNotFoundError("内置演示数据文件不存在：deploy/samples/Sample-data.xlsx")
    with path.open("rb") as stream:
        storage = FileStorage(stream=stream, filename=path.name, name="file")
        return register_upload(storage, workspace_id)


def _ensure_sample_knowledge(database: Database, workspace_id: str) -> int:
    from .knowledge import save_entry

    existing_entries = {
        str((item.get("sample_seed") or {}).get("key") or ""): item
        for item in database.list("knowledge_entries", workspace_id=workspace_id, limit=5000)
        if (item.get("sample_seed") or {}).get("id") == SAMPLE_SEED_ID
    }
    payloads = [
        {
            "key": "profitability",
            "type": "metric",
            "name": "城市盈利状态",
            "alias": "盈利/亏损城市",
            "definition": "基于城市当前盈利状况字段识别经营健康度，必须结合订单规模、市占率、履约成本和补贴判断。",
            "notes": "样例中用于解释区域经营差异，不能外推为真实市场结论。",
        },
        {
            "key": "active_merchants",
            "type": "metric",
            "name": "活跃合作商家数",
            "alias": "商家供给",
            "definition": "城市当前可服务的活跃合作商家数量，用于衡量供给密度和履约承载能力。",
            "sql_template": "SUM(活跃合作商家数)",
        },
        {
            "key": "subsidy_rule",
            "type": "business_rule",
            "name": "补贴效率诊断规则",
            "rule_id": "IR-SUBSIDY-001",
            "description": "当城市补贴及营销/单高、但市占率或订单增速仍低时，应优先检查供给密度、履约成本和高价值用户占比。",
            "severity": "medium",
        },
        {
            "key": "analysis_context",
            "type": "context_note",
            "name": "即时零售经营分析背景",
            "topic": "即时零售经营分析背景",
            "content": "样例用于演示从数据接入、口径沉淀、指标审批到可信分析与报告交付的 Data Agent 核心主路径。",
            "tags": ["demo", "instant-retail"],
        },
    ]
    created = 0
    for payload in payloads:
        key = payload.pop("key")
        current = existing_entries.get(key)
        desired = {**payload, "sample_seed": {"id": SAMPLE_SEED_ID, "key": key}}
        if current:
            if any(current.get(field) != value for field, value in desired.items()):
                save_entry(desired, workspace_id, current["id"])
            continue
        save_entry(desired, workspace_id)
        created += 1
    return created


def _ensure_sample_semantic(
    database: Database, workspace_id: str, source: dict[str, Any], actor_id: str,
) -> list[str]:
    from .semantic import save_metric, save_model

    created: list[str] = []
    existing_model = next(
        (
            item for item in database.list("semantic_models", workspace_id=workspace_id, limit=5000)
            if (item.get("sample_seed") or {}).get("id") == SAMPLE_SEED_ID
        ),
        None,
    )
    table_name = str((source.get("tables") or [{}])[0].get("name") or "t_10城数据包")
    if existing_model:
        model = existing_model
    else:
        model = save_model(
            database,
            {
                "source_id": source["id"],
                "name": "即时零售城市经营模型",
                "description": "围绕城市、省份、盈利状态和活跃商家供给构建的演示语义模型。",
                "table": table_name,
                "grain": "城市",
                "entities": [{"name": "城市", "column": "城市", "type": "primary", "label": "城市"}],
                "dimensions": [
                    {"name": "城市", "column": "城市", "type": "categorical", "label": "城市"},
                    {"name": "省份", "column": "省份", "type": "categorical", "label": "省份"},
                    {
                        "name": "城市当前盈利状况", "column": "城市当前盈利状况",
                        "type": "categorical", "label": "盈利状态",
                    },
                ],
                "measures": [
                    {
                        "name": "active_merchants", "column": "活跃合作商家数",
                        "aggregation": "sum", "label": "活跃合作商家数",
                    },
                ],
            },
            workspace_id,
            actor_id,
        )
        model = database.patch(
            "semantic_models", model["id"], {"sample_seed": {"id": SAMPLE_SEED_ID, "version": 1}},
            workspace_id=workspace_id,
        ) or model
        created.append("semantic_model")

    existing_metric = next(
        (
            item for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
            if item.get("model_id") == model["id"] and item.get("name") == "active_merchants_total"
        ),
        None,
    )
    if not existing_metric:
        metric = save_metric(
            database,
            {
                "model_id": model["id"],
                "name": "active_merchants_total",
                "label": "活跃合作商家总数",
                "description": "样例经营分析中的供给规模指标。",
                "measure": "active_merchants",
                "aliases": ["商家供给", "活跃商家"],
                "unit": "个",
                "format": "integer",
                "status": "approved",
            },
            workspace_id,
            actor_id,
        )
        database.patch(
            "semantic_metrics", metric["id"], {"sample_seed": {"id": SAMPLE_SEED_ID, "version": 1}},
            workspace_id=workspace_id,
        )
        created.append("semantic_metric")
    return created


def _attach_sample_to_active_session(
    database: Database, workspace_id: str, source_id: str,
) -> None:
    sessions = database.list("sessions", workspace_id=workspace_id, limit=5000)
    session = next((item for item in sessions if item.get("status") == "active"), sessions[0] if sessions else None)
    if not session:
        return
    source_ids = list(dict.fromkeys([source_id, *(str(item) for item in session.get("source_ids") or [])]))
    database.patch(
        "sessions", session["id"], {"source_ids": source_ids},
        workspace_id=workspace_id,
    )
