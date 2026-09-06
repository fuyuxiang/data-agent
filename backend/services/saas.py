from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage

from ..core.database import Database, utcnow


DEFAULT_TENANT_ID = "tenant_default"
DEFAULT_SUBSCRIPTION_ID = "sub_default_enterprise_trial"
SAMPLE_SEED_ID = "instant_retail_city_pack"


DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "id": "starter",
        "name": "团队版",
        "positioning": "单部门把数据问答、口径治理和报告交付跑通。",
        "price_model": "按工作空间订阅",
        "features": [
            "data_sources", "governed_agent", "knowledge_base", "semantic_layer",
            "dashboards", "result_delivery",
        ],
        "limits": {
            "workspaces": 1, "members": 5, "sources": 10,
            "knowledge_entries": 200, "semantic_metrics": 50,
            "dashboards": 3, "workflows": 0, "schedules": 0, "hooks": 0,
            "monthly_agent_runs": 100,
        },
        "value_points": ["可信问数", "指标口径沉淀", "标准报告导出"],
    },
    {
        "id": "growth",
        "name": "企业版",
        "positioning": "多部门共享指标资产，自动化生产经营分析报告。",
        "price_model": "按租户订阅",
        "features": [
            "data_sources", "governed_agent", "knowledge_base", "semantic_layer",
            "dashboards", "result_delivery", "automation", "feishu_bot",
            "mcp_integrations", "warehouse",
        ],
        "limits": {
            "workspaces": 8, "members": 80, "sources": 200,
            "knowledge_entries": 5000, "semantic_metrics": 800,
            "dashboards": 50, "workflows": 80, "schedules": 80, "hooks": 80,
            "monthly_agent_runs": 5000,
        },
        "value_points": ["跨团队复用", "自动化月报/周报", "协同机器人入口"],
    },
    {
        "id": "enterprise",
        "name": "旗舰版",
        "positioning": "集团级私有化/专有云部署，覆盖治理、审计、调度和结果交付闭环。",
        "price_model": "年度合同",
        "features": [
            "data_sources", "governed_agent", "knowledge_base", "semantic_layer",
            "dashboards", "result_delivery", "automation", "feishu_bot",
            "mcp_integrations", "warehouse", "workspace_governance", "audit",
            "lifecycle_management",
        ],
        "limits": {
            "workspaces": None, "members": None, "sources": None,
            "knowledge_entries": None, "semantic_metrics": None,
            "dashboards": None, "workflows": None, "schedules": None, "hooks": None,
            "monthly_agent_runs": None,
        },
        "value_points": ["权限与审计", "大数据引擎接入", "企业级交付与运维"],
    },
]


SOLUTION_CATALOG: list[dict[str, Any]] = [
    {
        "id": "decision-intelligence",
        "name": "经营分析 Agent",
        "target_customer": "经营管理、财务分析、区域运营团队",
        "customer_problem": "数据分散、口径不一致、分析结果难复核，临时经营问题响应慢。",
        "product_flow": [
            "接入经营数据源", "绑定业务知识与指标口径", "在受治理会话中确认分析目标",
            "Agent 自主查询与验证", "发布可追溯结论与报告",
        ],
        "required_features": ["data_sources", "knowledge_base", "semantic_layer", "governed_agent", "result_delivery"],
        "success_metrics": ["临时分析出稿时间", "可追溯结论占比", "重复口径问题减少率"],
    },
    {
        "id": "metric-governance",
        "name": "指标口径治理与数据问答",
        "target_customer": "数据治理、BI、业务中台团队",
        "customer_problem": "同名指标多套算法，问数结果不可解释，数据权限难以贯穿到 AI 使用场景。",
        "product_flow": [
            "登记数据资产", "沉淀指标/规则/背景知识", "建立语义模型",
            "审批核心指标", "通过自然语言问数并保留 SQL 与证据",
        ],
        "required_features": ["data_sources", "knowledge_base", "semantic_layer", "audit"],
        "success_metrics": ["核心指标覆盖率", "问数一次命中率", "审计可追溯率"],
    },
    {
        "id": "report-factory",
        "name": "自动化分析报告工厂",
        "target_customer": "例行周报、月报、专题复盘生产团队",
        "customer_problem": "固定报告大量手工复制粘贴，图表和结论复核成本高，交付格式不标准。",
        "product_flow": [
            "配置数据组合", "编排分析工作流", "加入审批与失败重试",
            "自动生成看板/Word/图片", "邮件或飞书触达业务方",
        ],
        "required_features": ["automation", "dashboards", "result_delivery", "feishu_bot"],
        "success_metrics": ["报告生产时长", "人工复制步骤减少量", "定时交付成功率"],
    },
]


def ensure_saas_baseline(database: Database, actor_id: str = "system") -> dict[str, Any]:
    """Create the minimum SaaS control-plane records around existing workspaces."""

    for plan in DEFAULT_PLANS:
        database.put_if_absent("plans", plan)

    tenant, _ = database.put_if_absent(
        "tenants",
        {
            "id": DEFAULT_TENANT_ID,
            "name": "默认客户",
            "status": "active",
            "primary_solution_id": "decision-intelligence",
            "created_by": actor_id,
        },
    )
    if actor_id and actor_id != "system":
        database.put_if_absent(
            "tenant_members",
            {
                "id": f"{DEFAULT_TENANT_ID}:{actor_id}",
                "tenant_id": DEFAULT_TENANT_ID,
                "user_id": actor_id,
                "role": "owner",
                "enabled": True,
            },
        )
    database.put_if_absent(
        "subscriptions",
        {
            "id": DEFAULT_SUBSCRIPTION_ID,
            "tenant_id": DEFAULT_TENANT_ID,
            "plan_id": "enterprise",
            "status": "trialing",
            "trial": True,
            "current_period_start": utcnow(),
            "current_period_end": _future(days=30),
            "entitlement_source": "local-default",
        },
    )

    patched = 0
    for workspace in database.list("workspaces", include_archived=True, limit=5000):
        if not workspace.get("tenant_id"):
            database.patch("workspaces", workspace["id"], {"tenant_id": DEFAULT_TENANT_ID})
            patched += 1
    return {"tenant": tenant, "patched_workspaces": patched}


def product_status(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    ensure_saas_baseline(database, actor_id)
    return {
        "solutions": SOLUTION_CATALOG,
        "plans": plans(database),
        "entitlements": workspace_entitlements(database, workspace_id),
        "onboarding": onboarding_status(database, workspace_id),
        "methodology": [
            {"id": "connect", "name": "接入", "description": "登记受控数据源，明确权限和数据表范围。"},
            {"id": "define", "name": "定义", "description": "沉淀指标、业务规则和语义模型，统一分析口径。"},
            {"id": "ask", "name": "分析", "description": "先确认需求理解，再由 Agent 查询、计算和解释。"},
            {"id": "validate", "name": "验证", "description": "保留 SQL、证据、质量门禁和运行日志，避免伪造成果。"},
            {"id": "deliver", "name": "交付", "description": "发布看板、Word、图片和邮件/飞书触达。"},
            {"id": "automate", "name": "自动化", "description": "把高频分析沉淀为工作流、调度和团队协作。"},
        ],
        "actor_id": actor_id,
    }


def plans(database: Database) -> list[dict[str, Any]]:
    ensure_saas_baseline(database)
    stored = {item["id"]: item for item in database.list("plans", limit=5000)}
    return [stored.get(plan["id"], plan) for plan in DEFAULT_PLANS]


def workspace_entitlements(database: Database, workspace_id: str) -> dict[str, Any]:
    ensure_saas_baseline(database)
    workspace = database.get("workspaces", workspace_id) or database.get("workspaces", "default") or {}
    tenant_id = str(workspace.get("tenant_id") or DEFAULT_TENANT_ID)
    subscriptions = [
        item for item in database.list("subscriptions", limit=5000)
        if item.get("tenant_id") == tenant_id and item.get("status") in {"active", "trialing"}
    ]
    subscription = subscriptions[0] if subscriptions else database.get("subscriptions", DEFAULT_SUBSCRIPTION_ID)
    plan_id = str((subscription or {}).get("plan_id") or "enterprise")
    plan = database.get("plans", plan_id) or next(item for item in DEFAULT_PLANS if item["id"] == "enterprise")
    active = (subscription or {}).get("status") in {"active", "trialing"}
    return {
        "tenant_id": tenant_id,
        "tenant_name": (database.get("tenants", tenant_id) or {}).get("name", "默认客户"),
        "workspace_id": workspace.get("id", workspace_id),
        "plan": {
            "id": plan["id"], "name": plan["name"], "positioning": plan.get("positioning", ""),
            "price_model": plan.get("price_model", ""),
        },
        "subscription": {
            "id": (subscription or {}).get("id", ""),
            "status": (subscription or {}).get("status", "inactive"),
            "trial": bool((subscription or {}).get("trial", False)),
            "current_period_end": (subscription or {}).get("current_period_end", ""),
        },
        "features": sorted(str(item) for item in plan.get("features") or []) if active else [],
        "limits": plan.get("limits") or {} if active else {},
    }


def assert_workspace_limit(database: Database, tenant_id: str, *, adding: int = 1) -> None:
    ensure_saas_baseline(database)
    subscriptions = [
        item for item in database.list("subscriptions", limit=5000)
        if item.get("tenant_id") == tenant_id and item.get("status") in {"active", "trialing"}
    ]
    subscription = subscriptions[0] if subscriptions else database.get("subscriptions", DEFAULT_SUBSCRIPTION_ID)
    if (subscription or {}).get("status") not in {"active", "trialing"}:
        raise PermissionError("当前订阅未激活，无法创建工作空间")
    plan_id = str((subscription or {}).get("plan_id") or "enterprise")
    plan = database.get("plans", plan_id) or next(item for item in DEFAULT_PLANS if item["id"] == "enterprise")
    limit = (plan.get("limits") or {}).get("workspaces")
    if limit is None:
        return
    current = len([
        item for item in database.list("workspaces", limit=5000)
        if str(item.get("tenant_id") or DEFAULT_TENANT_ID) == tenant_id
    ])
    if current + max(0, adding) > int(limit):
        raise PermissionError(f"当前套餐 workspaces 上限为 {limit}，请升级套餐或归档空闲工作空间")


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
    dashboard_count = len(database.list("dashboards", workspace_id=workspace_id, limit=5000))
    workflow_count = len([
        item for item in database.list("workflows", workspace_id=workspace_id, limit=5000)
        if item.get("status") == "published"
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
        {
            "id": "deliver_dashboard", "name": "形成可交付成果",
            "description": "发布看板、报告或可发送的分析附件。",
            "done": dashboard_count > 0, "count": dashboard_count, "route": "dashboards",
        },
        {
            "id": "automate_repeat", "name": "沉淀自动化流程",
            "description": "把高频分析固化为工作流、调度和协作任务。",
            "done": workflow_count > 0, "count": workflow_count, "route": "automation",
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


def assert_feature_enabled(database: Database, workspace_id: str, feature: str) -> dict[str, Any]:
    entitlements = workspace_entitlements(database, workspace_id)
    if feature not in set(entitlements.get("features") or []):
        raise PermissionError(f"当前套餐未开通功能：{feature}")
    return entitlements


def assert_collection_limit(
    database: Database,
    workspace_id: str,
    *,
    limit_key: str,
    collection: str,
    adding: int = 1,
) -> None:
    entitlements = workspace_entitlements(database, workspace_id)
    limit = (entitlements.get("limits") or {}).get(limit_key)
    if limit is None:
        return
    current = len(database.list(collection, workspace_id=workspace_id, limit=5000))
    if current + max(0, adding) > int(limit):
        raise PermissionError(f"当前套餐 {limit_key} 上限为 {limit}，请升级套餐或清理存量记录")


def assert_limit_available(
    database: Database,
    workspace_id: str,
    *,
    limit_key: str,
    current_count: int,
    adding: int = 1,
) -> None:
    entitlements = workspace_entitlements(database, workspace_id)
    limit = (entitlements.get("limits") or {}).get(limit_key)
    if limit is None:
        return
    if current_count + max(0, adding) > int(limit):
        raise PermissionError(f"当前套餐 {limit_key} 上限为 {limit}，请升级套餐或清理存量记录")


def assert_agent_run_limit(database: Database, workspace_id: str, *, adding: int = 1) -> None:
    current_count = len(database.list("agent_runs", workspace_id=workspace_id, limit=5000))
    current_count += len(database.list("workflow_runs", workspace_id=workspace_id, limit=5000))
    assert_limit_available(
        database, workspace_id,
        limit_key="monthly_agent_runs", current_count=current_count, adding=adding,
    )


def seed_demo_workspace(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    ensure_saas_baseline(database, actor_id)
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


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


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

    existing_keys = {
        str((item.get("sample_seed") or {}).get("key") or "")
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
            "content": "样例用于演示从数据接入、口径沉淀、指标审批到受治理分析交付的 SaaS 主路径。",
            "tags": ["demo", "instant-retail"],
        },
    ]
    created = 0
    for payload in payloads:
        key = payload.pop("key")
        if key in existing_keys:
            continue
        save_entry({**payload, "sample_seed": {"id": SAMPLE_SEED_ID, "key": key}}, workspace_id)
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


def _attach_sample_to_active_session(database: Database, workspace_id: str, source_id: str) -> None:
    sessions = database.list("sessions", workspace_id=workspace_id, limit=5000)
    session = next((item for item in sessions if item.get("status") == "active"), sessions[0] if sessions else None)
    if not session:
        return
    source_ids = list(dict.fromkeys([source_id, *(str(item) for item in session.get("source_ids") or [])]))
    database.patch("sessions", session["id"], {"source_ids": source_ids}, workspace_id=workspace_id)
