from __future__ import annotations

import io
from typing import Any

from werkzeug.datastructures import FileStorage

from ..core.database import Database
from . import demo_sales

SAMPLE_SEED_ID = demo_sales.SAMPLE_SEED_ID
DEPLOYMENT_FEATURES = frozenset({
    "data_sources", "governed_agent", "knowledge_base", "semantic_layer",
    "result_delivery", "mcp_integrations", "warehouse", "workspace_governance",
    "audit", "lifecycle_management", "skills", "agents", "library",
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
    """Three steps, in the order a new user actually needs them."""
    source_count = len(database.list("sources", workspace_id=workspace_id, limit=5000))
    metric_count = len([
        item for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
        if item.get("status") == "approved"
    ])
    from ..services.models import public_provider

    model_ready = False
    for provider in database.list("providers", workspace_id=workspace_id, limit=5000):
        if provider.get("enabled", True) and str(public_provider(provider).get("status") or "") == "ready":
            model_ready = True
            break
    steps = [
        {
            "id": "connect_model", "name": "连接模型",
            "description": "配置一个可用的模型服务，Agent 才能自主分析。",
            "done": model_ready, "route": "admin/models",
        },
        {
            "id": "add_data", "name": "添加数据",
            "description": "接入数据源，或先载入一套演示数据直接体验。",
            "done": source_count > 0, "count": source_count, "route": "admin/data",
        },
        {
            "id": "ask_question", "name": "开始提问",
            "description": "在工作台用业务语言提问，系统会自动选择技能并给出可信结果。",
            "done": bool(database.list("agent_runs", workspace_id=workspace_id, limit=1)),
            "route": "workbench",
        },
    ]
    required = [item for item in steps if item["id"] != "ask_question"]
    return {
        "workspace_id": workspace_id,
        "complete": all(item["done"] for item in required),
        "score": round(sum(1 for item in required if item["done"]) / len(required), 4),
        "steps": steps,
        "next_step": next((item for item in required if not item["done"]), None),
        "metric_count": metric_count,
        "demo_available": True,
        "sample_seed_id": SAMPLE_SEED_ID,
    }


# --------------------------------------------------------------------------- #
# Demo data
# --------------------------------------------------------------------------- #

def _existing_sample_source(database: Database, workspace_id: str) -> dict[str, Any] | None:
    for source in database.list("sources", workspace_id=workspace_id, limit=5000):
        seed = source.get("sample_seed") or {}
        if seed.get("id") == SAMPLE_SEED_ID:
            return source
    return None


def _register_sample_source(workspace_id: str) -> dict[str, Any]:
    from .datasets import register_upload

    frame = demo_sales.build_frame()
    payload = demo_sales.to_csv_bytes(frame)
    stream = FileStorage(
        stream=io.BytesIO(payload),
        # The upload path is validated with secure_filename, which strips
        # non-ASCII characters; the display name is set afterwards.
        filename="sales_monthly.csv",
        name="file",
    )
    record = register_upload(stream, workspace_id)
    return record


def _ensure_sample_source(database: Database, workspace_id: str) -> tuple[dict[str, Any], list[str]]:
    existing = _existing_sample_source(database, workspace_id)
    if existing is not None:
        return existing, []
    source = _register_sample_source(workspace_id)
    from ..core.database import utcnow

    frame = demo_sales.build_frame()
    patched = database.patch(
        "sources",
        source["id"],
        {
            "name": "即时零售月度销售样例",
            "description": (
                f"内置演示数据：{frame['统计年月'].nunique()} 个自然月的销售事实，"
                f"覆盖 {frame['区域'].nunique()} 个区域、{frame['城市'].nunique()} 座城市、"
                f"{frame['品类'].nunique()} 个品类与 {frame['渠道'].nunique()} 个渠道。"
            ),
            "classification": "internal",
            "sensitivity": "internal",
            "sample_seed": {"id": SAMPLE_SEED_ID, "version": 2},
            "last_refreshed_at": utcnow(),
        },
        workspace_id=workspace_id,
    )
    return (patched or source), ["source"]


def _ensure_sample_knowledge(database: Database, workspace_id: str) -> int:
    from .knowledge import save_entry

    existing = {
        str((item.get("sample_seed") or {}).get("key") or ""): item
        for item in database.list("knowledge_entries", workspace_id=workspace_id, limit=5000)
        if (item.get("sample_seed") or {}).get("id") == SAMPLE_SEED_ID
    }
    created = 0
    for payload in demo_sales.knowledge_payloads():
        key = payload.pop("key")
        current = existing.get(key)
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
    tables = source.get("tables") or []
    table_name = str(tables[0].get("name") or tables[0].get("source_name") or "") if tables else ""
    if not table_name:
        return created

    payload = demo_sales.semantic_model_payload(table_name)
    existing_model = next(
        (
            item for item in database.list("semantic_models", workspace_id=workspace_id, limit=5000)
            if (item.get("sample_seed") or {}).get("id") == SAMPLE_SEED_ID
        ),
        None,
    )
    if existing_model:
        model = existing_model
    else:
        model = save_model(
            database, {**payload, "source_id": source["id"]}, workspace_id, actor_id,
        )
        model = database.patch(
            "semantic_models", model["id"],
            {"sample_seed": {"id": SAMPLE_SEED_ID, "version": 2}}, workspace_id=workspace_id,
        ) or model
        created.append("semantic_model")

    by_name = {
        str(item.get("name")): item
        for item in database.list("semantic_metrics", workspace_id=workspace_id, limit=5000)
        if item.get("model_id") == model["id"]
    }
    # Atomic metrics must exist before derived ones reference them.
    for metric in sorted(demo_sales.metric_payloads(), key=lambda item: item.get("metric_type") != "atomic"):
        name = str(metric["name"])
        desired = {key: value for key, value in metric.items() if key != "status"}
        if name in by_name:
            continue
        saved = save_metric(
            database, {**desired, "model_id": model["id"], "status": "approved"},
            workspace_id, actor_id,
        )
        database.patch(
            "semantic_metrics", saved["id"],
            {"sample_seed": {"id": SAMPLE_SEED_ID, "version": 2}}, workspace_id=workspace_id,
        )
        created.append("semantic_metric")
    return created


def _attach_sample_to_active_session(database: Database, workspace_id: str, source_id: str) -> None:
    sessions = database.list("sessions", workspace_id=workspace_id, limit=5000)
    session = next(
        (item for item in sessions if item.get("status") == "active"),
        sessions[0] if sessions else None,
    )
    if not session:
        return
    source_ids = list(dict.fromkeys([source_id, *(str(item) for item in session.get("source_ids") or [])]))
    database.patch(
        "sessions", session["id"], {"source_ids": source_ids}, workspace_id=workspace_id,
    )


def seed_demo_workspace(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    """Idempotently load the demo data set, its metrics and its knowledge."""
    source, created = _ensure_sample_source(database, workspace_id)
    created += ["knowledge_entry"] * _ensure_sample_knowledge(database, workspace_id)
    created += _ensure_sample_semantic(database, workspace_id, source, actor_id)
    _attach_sample_to_active_session(database, workspace_id, source["id"])
    ensure_super_agent(database, workspace_id, actor_id)
    database.audit(
        "product.demo_seeded", workspace_id=workspace_id, actor=actor_id,
        object_type="sample_seed", object_id=SAMPLE_SEED_ID,
        detail={"created": created},
    )
    return {
        "created": created,
        "source": source,
        "summary": demo_sales.summary(demo_sales.build_frame()),
        "onboarding": onboarding_status(database, workspace_id),
    }


# --------------------------------------------------------------------------- #
# Super agent
# --------------------------------------------------------------------------- #

SUPER_AGENT_ID = "agent-superskill"
SUPER_AGENT_NAME = "数擎超级智能体"
SUPER_AGENT_INSTRUCTION = (
    "你是企业数据智能体。用户会提出业务问题，你负责理解问题、选择正确的能力、"
    "调用企业数据得出可靠结论。\n\n"
    "工作方式：\n"
    "1. 有正式指标时优先用指标定义，保证口径一致；没有对应指标时再做探索性分析。\n"
    "2. 结论先行，再给支撑数据。每个数字都要能追溯。\n"
    "3. 数据不足以回答时直接说明缺什么，不用估算填补。\n"
    "4. 区分「数据表明的」和「推测的」，推测必须标注。\n"
    "5. 不展示内部推理过程，只给结论、证据和下一步建议。"
)


def ensure_super_agent(database: Database, workspace_id: str, actor_id: str) -> dict[str, Any]:
    """Create the default entry agent if the workspace does not have one yet."""
    existing = database.get("agent_definitions", SUPER_AGENT_ID, workspace_id=workspace_id)
    if existing:
        return existing
    from ..core.database import utcnow

    return database.put("agent_definitions", {
        "id": SUPER_AGENT_ID,
        "workspace_id": workspace_id,
        "name": SUPER_AGENT_NAME,
        "description": "默认入口智能体，自动选择合适技能完成问数、分析、归因、预测与报告生成。",
        "instruction": SUPER_AGENT_INSTRUCTION,
        "icon": "sparkle",
        "tags": ["官方", "数据分析", "问数"],
        "source_ids": [],
        "knowledge_document_ids": [],
        "metric_ids": [],
        "mcp_server_ids": [],
        "provider_id": None,
        "skill_id": None,
        "skill_ids": [],
        "welcome": "问我任何企业数据问题，我会自动选择合适的技能并给出可核验的结论。",
        "suggested_questions": demo_sales.sample_questions()[:4],
        "visibility": "workspace",
        "status": "published",
        "version": 1,
        "created_by": actor_id,
        "published_at": utcnow(),
        "builtin": True,
    }, workspace_id=workspace_id)
