"""The seven-stage pipeline orchestrator."""

import json

import pytest

from app.core.config import Settings
from app.intent.recognizer import LlmCompletion
from app.observability.trace import Stage, load_trace
from app.pipeline.orchestrator import QueryOrchestrator, TurnStatus
from tests.security.factories import build_principals, user_id_for
from tests.semantic.factories import build_orders_dataset


class StubClient:
    """Queue of canned payloads, one per expected recognition call."""

    payloads_default = "{}"

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.last_user_prompt = ""

    def complete(self, system: str, user: str):
        self.calls += 1
        self.last_user_prompt = user
        content = self.payloads.pop(0) if self.payloads else self.payloads_default
        return LlmCompletion(
            content=content, model="stub", prompt_tokens=90, completion_tokens=15
        )


def _payload(**overrides) -> str:
    base = {
        "kind": "aggregate",
        "metrics": ["sales_revenue"],
        "dimensions": [],
        "filters": [],
        "time": {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "grain": "month",
            "expression": "本月",
        },
        "comparison": "none",
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.93},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def _settings(**overrides) -> Settings:
    base = {
        "clarify_confidence_threshold": 0.7,
        "clarify_max_rounds": 2,
        "max_result_rows": 1000,
        "cost_warn_rows": 10_000,
        "cost_reject_rows": 100_000,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _orchestrator(env, sample_conn, client, settings=None):
    return QueryOrchestrator(
        meta_session=env,
        sample_connection=sample_conn,
        llm_client=client,
        settings=settings or _settings(),
    )


def test_happy_path_answers_with_citation(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.ANSWERED
    assert outcome.answer.citation.metric.startswith("sales_revenue v3")
    assert outcome.answer.citation.data_updated_at


def test_all_seven_stages_are_traced(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    stages = [item.stage for item in load_trace(env, outcome.turn_id)]
    assert stages == [
        Stage.VERIFIED_RECALL.value,
        Stage.INTENT.value,
        Stage.SEMANTIC_RESOLVE.value,
        Stage.COMPILE.value,
        Stage.SECURITY.value,
        Stage.EXECUTE.value,
        Stage.ANSWER.value,
    ]


def test_intent_stage_records_model_and_tokens(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.INTENT.value
    )
    assert stage.model == "stub"
    assert stage.prompt_tokens == 90


def test_compiled_sql_is_in_the_trace(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.SECURITY.value
    )
    assert "SELECT" in stage.output_payload["sql"].upper()


def test_intent_snapshot_is_stored_for_replay(env, sample_conn):
    from app.observability.orm import TurnRow

    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    turn = env.get(TurnRow, outcome.turn_id)
    assert turn.intent_snapshot["metrics"] == ["sales_revenue"]


def test_low_confidence_returns_clarification_not_a_number(env, sample_conn):
    payload = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        user_id=user_id_for(env, "admin"), question="业绩怎么样", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.CLARIFYING
    assert outcome.answer is None
    assert outcome.clarifications


def test_clarifying_turn_stops_before_compilation(env, sample_conn):
    payload = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        user_id=user_id_for(env, "admin"), question="业绩怎么样", dataset_name="orders"
    )

    stages = {item.stage for item in load_trace(env, outcome.turn_id)}
    assert Stage.COMPILE.value not in stages


def test_unsupported_question_is_refused(env, sample_conn):
    payload = _payload(kind="unsupported", metrics=[], confidence={"overall": 0.1})
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        user_id=user_id_for(env, "admin"), question="帮我把这单改成已完成", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    assert outcome.refusal_reason


def test_permission_refusal_leaks_no_metadata(env, sample_conn):
    payload = _payload(metrics=["total_cost"])
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        user_id=user_id_for(env, "east_manager"), question="本月总成本", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    for leak in ("total_cost", "cost", "orders", "sample"):
        assert leak not in outcome.refusal_reason


def test_denied_columns_are_absent_from_the_prompt(env, sample_conn):
    """Recall-time invisibility: the model is never told the field exists."""
    client = StubClient(_payload())
    _orchestrator(env, sample_conn, client).ask(
        user_id=user_id_for(env, "east_manager"), question="本月销售额", dataset_name="orders"
    )

    assert "cost" not in client.last_user_prompt


def test_row_policy_appears_in_the_citation(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "east_manager"), question="本月销售额", dataset_name="orders"
    )

    permission_lines = [
        item for item in outcome.answer.citation.filters if item.source == "permission"
    ]
    assert permission_lines and "华东" in permission_lines[0].value


def test_verified_query_hit_degrades_to_full_pipeline(env, sample_conn):
    """VQ hits used to short-circuit straight to the cached SQL — that
    path skipped masking (P0-04: `SUM(masked_col)` would not be rewritten
    because `apply_masking` only touches exp.Alias). The fix is to fall
    through to the normal compile-and-secure path; cost is one model call
    per hit, which is the right trade against an exploitable bypass."""
    from datetime import date

    from app.intent.schema import (
        FieldConfidence,
        IntentKind,
        QueryIntent,
        TimeGrain,
        TimeRange,
    )
    from app.pipeline.verified import register

    verified_intent = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=["sales_revenue"],
        time=TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        confidence=FieldConfidence(overall=0.9),
        raw_question="本月销售额",
    )

    register(
        env,
        dataset_name="orders",
        question="本月销售额",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=verified_intent,
        created_by="admin",
    )
    env.flush()

    client = StubClient(_payload())
    outcome = _orchestrator(env, sample_conn, client).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.ANSWERED
    # Now uses the normal pipeline: one recognition call.
    assert client.calls == 1


def test_verified_query_hit_still_gets_row_policy(env, sample_conn):
    """The recall path must not become a permission bypass."""
    from datetime import date

    from app.intent.schema import (
        FieldConfidence,
        IntentKind,
        QueryIntent,
        TimeGrain,
        TimeRange,
    )
    from app.pipeline.verified import register

    verified_intent = QueryIntent(
        kind=IntentKind.AGGREGATE,
        dataset="orders",
        metrics=["sales_revenue"],
        time=TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        confidence=FieldConfidence(overall=0.9),
        raw_question="本月销售额",
    )

    register(
        env,
        dataset_name="orders",
        question="本月销售额",
        fixed_sql="SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        intent=verified_intent,
        created_by="admin",
    )
    env.flush()

    outcome = _orchestrator(env, sample_conn, StubClient()).ask(
        user_id=user_id_for(env, "east_manager"), question="本月销售额", dataset_name="orders"
    )

    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.SECURITY.value
    )
    assert "'EC'" in stage.output_payload["sql"]


def test_empty_result_does_not_produce_a_number(env, sample_conn):
    payload = _payload(
        time={
            "start": "2020-01-01",
            "end": "2020-01-31",
            "grain": "month",
            "expression": "2020年1月",
        }
    )
    outcome = _orchestrator(env, sample_conn, StubClient(payload)).ask(
        user_id=user_id_for(env, "admin"), question="2020年1月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.FAILED
    assert outcome.answer is None
    assert "没有数据" in outcome.refusal_reason


def test_slot_state_is_persisted_for_the_next_turn(env, sample_conn):
    orchestrator = _orchestrator(env, sample_conn, StubClient(_payload(), _payload()))
    first = orchestrator.ask(user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders")

    assert first.slot_state["metrics"] == ["sales_revenue"]

    second = orchestrator.ask(
        user_id=user_id_for(env, "admin"),
        question="那按省份看",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )
    assert second.status == TurnStatus.ANSWERED


def test_followup_receives_previous_slots_in_the_prompt(env, sample_conn):
    client = StubClient(_payload(), _payload())
    orchestrator = _orchestrator(env, sample_conn, client)
    first = orchestrator.ask(user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders")
    orchestrator.ask(
        user_id=user_id_for(env, "admin"),
        question="那华南呢",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )

    assert "上一轮" in client.last_user_prompt


def test_clarify_round_counting_falls_back_to_defaults(env, sample_conn):
    low = _payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9})
    client = StubClient(low, low, low)
    orchestrator = _orchestrator(
        env, sample_conn, client, _settings(clarify_max_rounds=1)
    )

    first = orchestrator.ask(user_id=user_id_for(env, "admin"), question="业绩怎么样", dataset_name="orders")
    assert first.status == TurnStatus.CLARIFYING

    second = orchestrator.ask(
        user_id=user_id_for(env, "admin"),
        question="业绩怎么样",
        conversation_id=first.conversation_id,
        dataset_name="orders",
    )

    # Round limit reached: answer, but the assumption must be visible.
    assert second.status == TurnStatus.ANSWERED
    assert second.answer.assumptions


def test_unpublished_dataset_is_refused(meta_session, sample_conn):
    build_orders_dataset(meta_session, published=False)
    build_principals(meta_session)

    outcome = _orchestrator(meta_session, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(meta_session, "admin"), question="本月销售额", dataset_name="orders"
    )
    assert outcome.status == TurnStatus.REFUSED


def test_recognition_failure_is_reported_as_failed(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient("这不是 JSON")).ask(
        user_id=user_id_for(env, "admin"), question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.FAILED
    stage = next(
        item for item in load_trace(env, outcome.turn_id) if item.stage == Stage.INTENT.value
    )
    assert stage.error


def test_inactive_user_is_refused_without_detail(env, sample_conn):
    outcome = _orchestrator(env, sample_conn, StubClient(_payload())).ask(
        user_id=user_id_for(env, "retired_user"), question="本月销售额", dataset_name="orders"
    )

    assert outcome.status == TurnStatus.REFUSED
    assert "orders" not in outcome.refusal_reason