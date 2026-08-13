"""Conversation, turn and per-stage Trace persistence."""

import pytest

from app.observability.orm import ConversationRow, TurnRow
from app.observability.trace import Stage, TraceRecorder, load_trace


@pytest.fixture
def turn(meta_session):
    conversation = ConversationRow(user_id=1, title="本月销售额")
    row = TurnRow(conversation=conversation, question="本月销售额")
    meta_session.add(row)
    meta_session.flush()
    return row


def test_record_persists_a_stage(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    recorder.record(Stage.INTENT, input_payload={"question": "本月销售额"})
    meta_session.flush()

    stages = load_trace(meta_session, turn.id)
    assert len(stages) == 1
    assert stages[0].stage == Stage.INTENT.value


def test_stages_keep_their_order(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    for stage in (Stage.INTENT, Stage.COMPILE, Stage.EXECUTE):
        recorder.record(stage, input_payload={})
    meta_session.flush()

    stages = load_trace(meta_session, turn.id)
    assert [item.sequence for item in stages] == [1, 2, 3]
    assert [item.stage for item in stages] == [
        Stage.INTENT.value,
        Stage.COMPILE.value,
        Stage.EXECUTE.value,
    ]


def test_token_usage_is_recorded(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    recorder.record(
        Stage.INTENT,
        input_payload={},
        output_payload={"metric": "sales_revenue"},
        model="claude-opus-5",
        prompt_tokens=1200,
        completion_tokens=80,
    )
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    assert stage.model == "claude-opus-5"
    assert stage.prompt_tokens == 1200
    assert stage.completion_tokens == 80


def test_stage_timer_records_elapsed_and_output(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)

    with recorder.stage_timer(Stage.COMPILE, {"metric": "sales_revenue"}) as span:
        span.output = {"sql": "SELECT 1"}
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    assert stage.elapsed_ms >= 0
    assert stage.output_payload == {"sql": "SELECT 1"}
    assert stage.error is None


def test_stage_timer_records_error_and_reraises(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)

    with pytest.raises(ValueError):
        with recorder.stage_timer(Stage.EXECUTE, {"sql": "SELECT 1"}):
            raise ValueError("boom")
    meta_session.flush()

    stage = load_trace(meta_session, turn.id)[0]
    # A failing stage is the one most worth having in Trace.
    assert "boom" in stage.error


def test_payloads_survive_nested_structures(meta_session, turn):
    recorder = TraceRecorder(meta_session, turn.id)
    payload = {"filters": [{"field": "region_code", "values": ["EC"]}], "confidence": 0.91}
    recorder.record(Stage.SEMANTIC_RESOLVE, input_payload=payload)
    meta_session.flush()

    assert load_trace(meta_session, turn.id)[0].input_payload == payload


def test_intent_snapshot_supports_replay(meta_session, turn):
    turn.intent_snapshot = {"kind": "aggregate", "metrics": ["sales_revenue"]}
    meta_session.flush()
    meta_session.expire(turn)

    assert meta_session.get(TurnRow, turn.id).intent_snapshot["metrics"] == ["sales_revenue"]


def test_trace_of_another_turn_is_not_returned(meta_session, turn):
    other = TurnRow(conversation_id=turn.conversation_id, question="上月销售额")
    meta_session.add(other)
    meta_session.flush()

    TraceRecorder(meta_session, turn.id).record(Stage.INTENT, input_payload={})
    TraceRecorder(meta_session, other.id).record(Stage.ANSWER, input_payload={})
    meta_session.flush()

    assert len(load_trace(meta_session, turn.id)) == 1
    assert load_trace(meta_session, other.id)[0].stage == Stage.ANSWER.value


def test_conversation_holds_structured_slot_state(meta_session):
    """M-19: multi-turn context is slots, not chat history."""
    conversation = ConversationRow(
        user_id=1,
        title="华东销售",
        slot_state={"metrics": ["sales_revenue"], "filters": {"region_code": ["EC"]}},
    )
    meta_session.add(conversation)
    meta_session.flush()
    meta_session.expire(conversation)

    stored = meta_session.get(ConversationRow, conversation.id)
    assert stored.slot_state["filters"]["region_code"] == ["EC"]