"""Tests for LLM snapshot recording in Trace (Step 5)."""

import pytest

from app.observability.trace import Stage, StageSpan


pytestmark = pytest.mark.no_db


class TestTraceRecorderLlmSnapshot:
    """Test LLM snapshot recording in trace."""

    def test_stage_span_has_llm_fields(self):
        """StageSpan supports model and token tracking."""
        span = StageSpan()
        assert span.model is None
        assert span.prompt_tokens == 0
        assert span.completion_tokens == 0

        # Can set LLM info
        span.model = "gpt-4o"
        span.prompt_tokens = 100
        span.completion_tokens = 50

        assert span.model == "gpt-4o"
        assert span.prompt_tokens == 100
        assert span.completion_tokens == 50

    def test_stage_span_output_field(self):
        """StageSpan output field for result capture."""
        span = StageSpan()
        assert span.output is None

        span.output = {"intent": "aggregate", "metrics": ["销售额"]}
        assert span.output["intent"] == "aggregate"

    def test_stage_enum_includes_intent(self):
        """Stage enum has INTENT for LLM recording."""
        assert hasattr(Stage, "INTENT")
        assert Stage.INTENT.value == "intent"


class TestRecognizeIntegration:
    """Integration tests for recognize() with optional tracing."""

    def test_recognize_without_recorder_is_compatible(self):
        """recognize() signature is backward compatible without recorder."""
        from app.intent.recognizer import recognize, LlmCompletion
        from app.semantic.model import DatasetDef

        # Mock client
        class MockClient:
            def complete(self, system: str, user: str) -> LlmCompletion:
                return LlmCompletion(
                    content='{"kind":"aggregate","metrics":[],"confidence":{"overall":0.9}}',
                    model="gpt-4o",
                    prompt_tokens=100,
                    completion_tokens=50,
                )

        dataset = DatasetDef(
            name="test", business_name="Test", grain="day",
            applicable_scenario="test", forbidden_scenario="",
            physical_table="schema.table",
            metrics=[], fields=[]
        )

        # Call without recorder parameter (backward compat)
        client = MockClient()
        intent, completion = recognize(client, dataset, "test question")

        assert completion.model == "gpt-4o"
        assert completion.prompt_tokens == 100
        assert completion.completion_tokens == 50
        assert intent is not None

    def test_recognize_accepts_recorder_parameter(self):
        """recognize() accepts optional recorder parameter."""
        from app.intent.recognizer import recognize, LlmCompletion
        from app.semantic.model import DatasetDef

        # Mock client
        class MockClient:
            def complete(self, system: str, user: str) -> LlmCompletion:
                return LlmCompletion(
                    content='{"kind":"aggregate","metrics":[],"confidence":{"overall":0.9}}',
                    model="gpt-4o-mini",
                    prompt_tokens=95,
                    completion_tokens=45,
                )

        dataset = DatasetDef(
            name="test", business_name="Test", grain="day",
            applicable_scenario="test", forbidden_scenario="",
            physical_table="schema.table",
            metrics=[], fields=[]
        )

        # Can call with recorder=None (same as omitting it)
        client = MockClient()
        intent, completion = recognize(client, dataset, "test question", recorder=None)

        assert completion.model == "gpt-4o-mini"
        assert completion.prompt_tokens == 95
        assert completion.completion_tokens == 45

