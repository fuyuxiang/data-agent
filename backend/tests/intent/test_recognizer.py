"""LLM recognizer + validation layer that gates the model output."""

import json

import pytest

from app.intent.recognizer import IntentRecognitionError, LlmCompletion, recognize
from app.intent.schema import ComparisonKind, IntentKind
from app.semantic.loader import load_dataset
from tests.semantic.factories import build_orders_dataset


class StubClient:
    """Returns a canned payload so the deterministic layer can be tested alone."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> LlmCompletion:
        self.calls.append((system, user))
        return LlmCompletion(
            content=self.content, model="stub", prompt_tokens=100, completion_tokens=20
        )


@pytest.fixture
def orders(meta_session):
    build_orders_dataset(meta_session)
    return load_dataset(meta_session, "orders")


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
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.9},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_recognize_returns_a_query_intent(orders):
    intent, completion = recognize(StubClient(_payload()), orders, "本月销售额")

    assert intent.kind == IntentKind.AGGREGATE
    assert intent.metrics == ["sales_revenue"]
    assert intent.time.expression == "本月"
    assert intent.raw_question == "本月销售额"
    assert completion.prompt_tokens == 100


def test_comparison_is_parsed(orders):
    intent, _ = recognize(StubClient(_payload(comparison="mom")), orders, "本月销售额环比")
    assert intent.comparison == ComparisonKind.MOM


def test_json_in_code_fence_is_accepted(orders):
    fenced = f"```json\n{_payload()}\n```"
    intent, _ = recognize(StubClient(fenced), orders, "本月销售额")
    assert intent.metrics == ["sales_revenue"]


def test_non_json_output_is_rejected(orders):
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient("我觉得你想问销售额"), orders, "本月销售额")


def test_unknown_metric_is_rejected(orders):
    """A hallucinated metric must not reach the compiler."""
    with pytest.raises(IntentRecognitionError) as excinfo:
        recognize(StubClient(_payload(metrics=["profit_rate_v9"])), orders, "利润率")
    assert "profit_rate_v9" in excinfo.value.reason


def test_unknown_dimension_is_rejected(orders):
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(_payload(dimensions=["city"])), orders, "各城市销售额")


def test_unknown_filter_field_is_rejected(orders):
    payload = _payload(
        filters=[{"field": "salesperson", "operator": "eq", "spoken_values": ["张三"]}]
    )
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(payload), orders, "张三的销售额")


def test_sql_in_output_is_rejected(orders):
    """The model has exactly one job and writing SQL is not it."""
    payload = _payload(assumptions=["SELECT SUM(amount) FROM orders"])
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(payload), orders, "本月销售额")


def test_unsupported_kind_passes_through(orders):
    payload = _payload(kind="unsupported", metrics=[], confidence={"overall": 0.1})
    intent, _ = recognize(StubClient(payload), orders, "帮我下单")

    # Refusal is decided downstream; recognition only reports what it saw.
    assert intent.kind == IntentKind.UNSUPPORTED


def test_missing_confidence_is_rejected(orders):
    payload = json.loads(_payload())
    del payload["confidence"]
    with pytest.raises(IntentRecognitionError):
        recognize(StubClient(json.dumps(payload)), orders, "本月销售额")


def test_filters_keep_spoken_values_unresolved(orders):
    """Value mapping is the next stage's job, not the model's."""
    payload = _payload(
        filters=[{"field": "region_code", "operator": "in", "spoken_values": ["华东"]}]
    )
    intent, _ = recognize(StubClient(payload), orders, "华东销售额")

    assert intent.filters[0].spoken_values == ["华东"]
    assert intent.filters[0].values == []


def test_time_absent_is_allowed(orders):
    payload = json.loads(_payload())
    payload["time"] = None
    intent, _ = recognize(StubClient(json.dumps(payload)), orders, "销售额")

    # Missing time triggers clarification later, not a recognition failure.
    assert intent.time is None


def test_slot_state_is_passed_to_the_prompt(orders):
    client = StubClient(_payload())
    recognize(client, orders, "那华南呢", slot_state={"metrics": ["sales_revenue"]})

    _, user = client.calls[0]
    assert "上一轮" in user