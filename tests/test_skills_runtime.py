"""Skill runtime: registry, resolver, executor, permissions and evaluation.

A Skill is a real runtime capability, not a prompt template. These tests pin
the behaviour that makes it one: discovery, deterministic selection, tool
narrowing, and permission filtering that happens *before* execution.
"""

from __future__ import annotations

import io

import pytest

from backend.skills.executor import SkillExecutor, effective_tool_set
from backend.skills.loader import load_builtin_skills
from backend.skills.models import FORMAL_AGENT_TOOLS, SkillError, skill_from_payload
from backend.skills.registry import SkillRegistry
from backend.skills.resolver import SkillResolver, extract_explicit, match_known

BUILTIN = load_builtin_skills()


# ---------------------------------------------------------------- 加载与注册


def test_ten_builtin_skills_load_with_real_instructions():
    assert set(BUILTIN) == {
        "data-query", "data-analysis", "attribution", "forecast", "excel-analysis",
        "visualization", "deep-research", "report", "ppt", "excel-export",
    }
    for definition in BUILTIN.values():
        assert definition.instruction.strip(), f"{definition.id} 缺少使用说明"
        assert definition.triggers, f"{definition.id} 缺少触发场景"
        assert definition.example_questions, f"{definition.id} 缺少示例问题"
        assert set(definition.allowed_tools) <= FORMAL_AGENT_TOOLS


def test_a_broken_package_does_not_take_the_workspace_down(tmp_path, monkeypatch):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "manifest.yaml").write_text(
        "id: good\nname: 可用技能\ndescription: d\ninstruction: 做事\ntriggers: [做事]\n"
        "example_questions: [做事？]\nallowed_tools: [query_data]\n",
        encoding="utf-8",
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    monkeypatch.setattr("backend.skills.loader.skill_dir", lambda: tmp_path)
    loaded = load_builtin_skills(tmp_path)
    assert "good" in loaded
    assert "broken" not in loaded


def test_workspace_skill_shadows_builtin_of_the_same_slug(app):
    from backend.core.database import utcnow

    database = app.extensions["meridian_db"]
    with app.app_context():
        database.put("skills", {
            "id": "skl_custom", "slug": "report", "workspace_id": "default",
            "name": "内部报告技能", "description": "受控版本", "status": "draft",
            "source": "workspace", "instruction": "只输出内部口径",
            "triggers": ["内部报告"], "example_questions": ["出一份内部报告"],
            "allowed_tools": ["read_tool_result"], "version": 1,
            "created_by": "tester", "created_at": utcnow(),
        }, workspace_id="default")
        registry = SkillRegistry(database, "default")
        assert registry.get("report").name == "内部报告技能"
        assert registry.get("report").editable is True
        assert registry.record_for("report")["id"] == "skl_custom"


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize("payload, message", [
    ({"name": "缺标识的技能"}, "技能标识"),
    ({"id": "Bad_Id", "name": "x"}, "技能标识"),
    ({"id": "ok-skill", "name": ""}, "技能名称"),
    ({"id": "ok-skill", "name": "x", "description": "d", "instruction": "i", "allowed_tools": ["rm_rf"]}, "不支持的工具"),
    ({"id": "ok-skill", "name": "x", "description": "d", "instruction": "i", "category": "不存在"}, "技能分类"),
    ({"id": "ok-skill", "name": "x", "description": "d"}, "使用说明"),
])
def test_invalid_skill_payloads_are_rejected_with_a_reason(payload, message):
    with pytest.raises(SkillError) as error:
        skill_from_payload(payload)
    assert message in str(error.value)


def test_a_draft_may_be_incomplete_but_cannot_be_published(client):
    """草稿允许不完整，发布才是那道闸。"""
    created = client.post("/api/skills", json={
        "id": "draft-skill", "name": "草稿技能",
        "description": "还没想好触发条件", "instruction": "先写着",
    })
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["item"]["status"] == "draft"
    rejected = client.post("/api/skills/draft-skill/publish")
    assert rejected.status_code == 400
    assert "触发场景" in rejected.get_json()["error"]


def test_skill_package_can_be_exported_and_imported(client):
    created = client.post("/api/skills", json={
        "id": "store-review", "name": "门店复盘", "description": "复盘门店销售",
        "instruction": "按门店查询并总结差异。", "triggers": ["门店复盘"],
        "allowed_tools": ["get_schema", "query_data"],
    })
    assert created.status_code == 201, created.get_json()
    exported = client.get("/api/skills/store-review/export")
    assert exported.status_code == 200
    assert client.delete("/api/skills/store-review").status_code == 200
    imported = client.post(
        "/api/skills/import",
        data={"file": (io.BytesIO(exported.data), "store-review.zip")},
        content_type="multipart/form-data",
    )
    assert imported.status_code == 201, imported.get_json()
    assert imported.get_json()["item"]["id"] == "store-review"


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize("question, expected", [
    ("本月销售额是多少？", "data-query"),
    ("华东销售同比怎么样？", "data-analysis"),
    ("为什么华东销售下降？", "attribution"),
    ("哪些商品表现异常？", "data-analysis"),
    ("预测下个月销售额", "forecast"),
    ("生成经营分析报告", "report"),
    ("生成经营分析 PPT", "ppt"),
    ("把这份明细导出成 Excel", "excel-export"),
    ("帮我做个趋势图", "visualization"),
    ("深度研究华东市场增长机会", "deep-research"),
])
def test_documented_questions_resolve_to_the_right_skill(question, expected):
    resolution = SkillResolver(list(BUILTIN.values())).resolve(question)
    assert expected in {item.id for item in resolution.selected}, (
        question, [item.id for item in resolution.selected]
    )


def test_a_vague_question_selects_nothing_rather_than_guessing():
    """说不清的问题不该被硬塞给某个技能；没有匹配就是没有匹配。"""
    resolution = SkillResolver(list(BUILTIN.values())).resolve("分析一下华东销售。")
    assert resolution.selected == ()
    assert all(item.score < 4.0 for item in resolution.candidates)


def test_explicit_mention_beats_scoring():
    resolver = SkillResolver(list(BUILTIN.values()))
    resolution = resolver.resolve("随便看看", explicit=["预测分析"])
    assert [item.id for item in resolution.selected] == ["forecast"]


def test_inline_mention_survives_a_greedy_capture():
    """`@预测分析 预测下个月` 里的贪婪匹配必须能收敛回技能名。"""
    assert match_known(extract_explicit("@预测分析 预测下个月")[0], list(BUILTIN.values())) == "forecast"
    assert match_known(extract_explicit("@Excel 分析 导出")[0], list(BUILTIN.values())) == "excel-analysis"
    assert match_known(extract_explicit("@不存在的技能")[0], list(BUILTIN.values())) == ""


def test_resolver_explains_itself():
    resolution = SkillResolver(list(BUILTIN.values())).resolve("为什么华东销售下降？")
    top = resolution.candidates[0]
    assert top.definition.id == "attribution"
    assert top.reasons


# ---------------------------------------------------------------- 执行


def test_skill_tool_set_is_narrowed_and_gets_metric_tools():
    tools = effective_tool_set(BUILTIN["forecast"])
    assert "query_data" in tools
    # 触达原始数据后，受治理的指标工具必须自动可用
    assert {"list_semantic_metrics", "query_metric"} <= tools
    assert "warehouse_spark_submit" not in tools


def test_a_skill_without_tools_is_guidance_only():
    from backend.skills.models import SkillDefinition

    guidance = SkillDefinition(id="tone", name="语气", instruction="说人话")
    assert effective_tool_set(guidance) == effective_tool_set(
        SkillDefinition(id="other", name="别的", instruction="别的")
    )


def test_unavailable_runtime_tools_are_removed():
    tools = effective_tool_set(BUILTIN["attribution"], runtime_tools=["query_data", "get_schema"])
    assert "run_analysis" not in tools
    assert {"get_schema", "query_data"} <= tools
    # 澄清、读结果与校验任何时候都要能用
    assert {"ask_user", "read_tool_result", "validate_result"} <= tools


def test_multiple_skills_union_their_tools():
    from backend.skills.context import SkillContext

    executor = SkillExecutor(SkillContext(workspace_id="default", actor_id="t", run_id="r", session_id="s"))
    executions = executor.prepare([BUILTIN["data-query"], BUILTIN["visualization"]])
    combined = SkillExecutor.combined_tools(executions)
    assert {"query_data", "query_metric", "generate_chart", "select_chart"} <= combined
    assert len(SkillExecutor.model_blocks(executions)) == 2


# ---------------------------------------------------------------- 权限


def test_skill_requiring_an_unreachable_source_is_hidden():
    from backend.skills.models import SkillDefinition
    from backend.skills.permissions import AvailableResources, filter_visible

    blocked = SkillDefinition(
        id="needs-secret", name="需要内部数据", instruction="做事",
        triggers=["内部"], example_questions=["内部数据呢？"],
        allowed_tools=["query_data"], source_ids=("src_missing",),
    )
    open_skill = SkillDefinition(
        id="open", name="通用", instruction="做事",
        triggers=["通用"], example_questions=["通用问题"], allowed_tools=["query_data"],
    )
    visible = filter_visible(
        [blocked, open_skill],
        AvailableResources(source_ids=frozenset({"src_other"})),
    )
    assert [item.id for item in visible] == ["open"]


def test_metrics_are_a_hint_not_a_gate():
    """技能声明偏好某些指标，是为了口径一致，不该因为没有这些指标就消失。"""
    from backend.skills.permissions import skill_requirements

    assert not skill_requirements(BUILTIN["data-query"]).missing(
        __import__("backend.skills.permissions", fromlist=["AvailableResources"]).AvailableResources()
    )
    assert BUILTIN["data-query"].metric_names


# ---------------------------------------------------------------- 评测


def test_static_checks_reject_an_unusable_skill(app):
    from backend.skills.evaluator import static_checks

    from backend.skills.models import SkillDefinition

    with app.app_context():
        broken = SkillDefinition(id="x", name="空技能", instruction="", triggers=(), example_questions=())
        checks = {check.name: check.passed for check in static_checks(
            app.extensions["meridian_db"], "default", "tester", broken,
        )}
    assert checks["使用说明"] is False
    assert checks["描述"] is False
    assert checks["触发条件"] is False


def test_builtin_skills_pass_their_own_evaluation(app):
    from backend.skills.evaluator import evaluate

    with app.app_context():
        database = app.extensions["meridian_db"]
        registry = SkillRegistry(database, "default")
        for definition in BUILTIN.values():
            report = evaluate(database, "default", "tester", definition, registry=registry)
            assert report.passed, (definition.id, [c.detail for c in report.checks if not c.passed])
