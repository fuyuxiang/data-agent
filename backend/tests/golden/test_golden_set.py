"""End-to-end Golden Set runner over YAML cases."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.intent.recognizer import LlmCompletion, OpenAiCompatClient
from app.pipeline.orchestrator import QueryOrchestrator
from tests.golden.loader import GoldenCase, IntentExpectation, load_cases
from tests.golden.runner import run_case, run_clarify_followup

CASES_DIR = Path(__file__).parent / "cases"


class StubClient:
    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)

    def complete(self, system: str, user: str) -> LlmCompletion:
        content = self.payloads.pop(0) if self.payloads else "{}"
        return LlmCompletion(content=content, model="stub", prompt_tokens=90, completion_tokens=15)


def _mode() -> str:
    return os.environ.get("DATA_AGENT_GOLDEN_MODE", "stub")


def _settings() -> Settings:
    return Settings(
        clarify_confidence_threshold=0.7,
        clarify_max_rounds=2,
        max_result_rows=1000,
        cost_warn_rows=10_000,
        cost_reject_rows=100_000,
    )


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    return value


def _intent_payload(expected: IntentExpectation | None, status: str = "ANSWERED", clarify_kind: str | None = None) -> str:
    if expected is None:
        payload = {
            "kind": "unsupported",
            "metrics": [],
            "dimensions": [],
            "filters": [],
            "time": None,
            "comparison": "none",
            "confidence": {"overall": 0.1},
            "assumptions": [],
        }
    else:
        payload = {
            "kind": "ranking" if expected.top_n is not None else "aggregate",
            "metrics": [expected.metric] if expected.metric else [],
            "dimensions": list(expected.dimension),
            "filters": list(expected.filters),
            "time": None if expected.time is None else {
                "start": expected.time.start.isoformat(),
                "end": expected.time.end.isoformat(),
                "grain": expected.time.grain.value,
                "expression": expected.time.expression,
            },
            "comparison": expected.comparison.value if expected.comparison else "none",
            "confidence": _confidence_payload(status, clarify_kind),
            "assumptions": [],
        }
        if expected.top_n is not None:
            payload["sort"] = {"by": expected.metric, "descending": True, "limit": expected.top_n}
    return json.dumps(payload, ensure_ascii=False)


def _confidence_payload(status: str, clarify_kind: str | None) -> dict[str, float]:
    payload = {"overall": 0.92, "metric": 0.95, "time": 0.93, "dimension": 0.9}
    if status != "CLARIFYING":
        return payload
    if clarify_kind == "METRIC":
        payload["metric"] = 0.3
    elif clarify_kind == "DIMENSION":
        payload["dimension"] = 0.3
    elif clarify_kind == "TIME":
        payload["time"] = 0.3
    else:
        payload["overall"] = 0.3
    return payload


def _client_for(case: GoldenCase, mode: str) -> Any:
    if mode == "real":
        settings = get_settings()
        if not settings.llm_api_key:
            pytest.skip("DATA_AGENT_GOLDEN_MODE=real requires LLM_API_KEY")
        return OpenAiCompatClient(settings)
    if case.followup is not None:
        return StubClient(
            _intent_payload(case.expect.intent, case.expect.status, case.expect.clarify_kind),
            _intent_payload(case.followup.expect.intent, case.followup.expect.status, case.followup.expect.clarify_kind),
        )
    return StubClient(_intent_payload(case.expect.intent, case.expect.status, case.expect.clarify_kind))


def _to_outcome(raw: Any) -> dict[str, Any]:
    status = raw.status.value.upper()
    answer = raw.answer
    citation = []
    rows = []
    if answer is not None:
        rows = [dict(zip(answer.columns, row)) for row in answer.rows]
        if answer.citation is not None:
            citation = [
                {"kind": "metric", "text": answer.citation.metric},
                {"kind": "time", "text": answer.citation.time},
            ]
            citation.extend(
                {"kind": item.source if item.source == "permission" else "filter", "text": item.value}
                for item in answer.citation.filters
            )
    return {
        "status": status,
        "rows": rows,
        "citation": citation,
        "response_text": raw.refusal_reason or (answer.headline if answer else ""),
        "intent": None,
        "clarify_kind": raw.clarifications[0].kind.upper() if raw.clarifications else None,
        "options": [_plain(item) for item in raw.clarifications[0].options] if raw.clarifications else [],
    }


def _orchestrator(meta_session: Any, sample_conn: Any, client: Any):
    engine = QueryOrchestrator(
        meta_session=meta_session,
        sample_connection=sample_conn,
        llm_client=client,
        settings=_settings(),
    )

    def run(**kwargs: Any) -> dict[str, Any]:
        raw = engine.ask(
            username=kwargs["username"],
            question=kwargs["question"],
            dataset_name="orders",
            conversation_id=kwargs.get("conversation_id"),
        )
        outcome = _to_outcome(raw)
        outcome["conversation_id"] = raw.conversation_id
        return outcome

    return run


def _user_id_resolver(meta_session: Any):
    def resolve(username: str) -> int:
        from app.security.principal import load_principal

        return load_principal(meta_session, username).user_id

    return resolve


def _ids(cases: list[GoldenCase]) -> list[str]:
    mode = _mode()
    return [f"{case.id}-{mode}" for case in cases]


_CASES = load_cases(CASES_DIR) if CASES_DIR.exists() else []
_SIMPLE_CASES = [case for case in _CASES if case.followup is None]
_FOLLOWUP_CASES = [case for case in _CASES if case.followup is not None]


@pytest.mark.parametrize("case", _SIMPLE_CASES, ids=_ids(_SIMPLE_CASES))
def test_golden_case(
    case: GoldenCase,
    golden_env: Any,
    sample_conn: Any,
    ephemeral_policy: Any,
) -> None:
    client = _client_for(case, _mode())
    report = run_case(
        case,
        mode=_mode(),
        orchestrator=lambda **kwargs: _orchestrator(golden_env, sample_conn, client)(
            **kwargs,
            username=case.as_user,
        ),
        user_id_resolver=_user_id_resolver(golden_env),
        ephemeral_policy=ephemeral_policy,
    )
    assert report.status in {"PASS", "XFAIL", "SKIPPED"}, report.message


@pytest.mark.parametrize("case", _FOLLOWUP_CASES, ids=_ids(_FOLLOWUP_CASES))
def test_golden_clarify_followup(
    case: GoldenCase,
    golden_env: Any,
    sample_conn: Any,
    ephemeral_policy: Any,
) -> None:
    client = _client_for(case, _mode())
    conversation_id: dict[str, int | None] = {"value": None}

    def orchestrator(**kwargs: Any) -> dict[str, Any]:
        outcome = _orchestrator(golden_env, sample_conn, client)(
            **kwargs,
            username=kwargs.get("username", case.as_user),
            conversation_id=conversation_id["value"],
        )
        conversation_id["value"] = outcome["conversation_id"]
        return outcome

    first, second = run_clarify_followup(
        case,
        mode=_mode(),
        orchestrator=orchestrator,
        user_id_resolver=_user_id_resolver(golden_env),
        ephemeral_policy=ephemeral_policy,
    )
    assert first.status in {"PASS", "XFAIL", "SKIPPED"}, first.message
    assert second.status in {"PASS", "XFAIL", "SKIPPED"}, second.message
