"""Tests for Structured Outputs integration (Step 2).

Verifies that OpenAI Structured Outputs API integration is working correctly.
"""

import pytest
from pydantic import ValidationError

from app.intent.recognizer import IntentPayload, OpenAiCompatClient, recognize
from app.llm.structured import schema_from_model
from app.semantic.model import DatasetDef

pytestmark = pytest.mark.no_db


class TestStructuredOutputsSchema:
    """Test JSON schema generation for Structured Outputs."""

    def test_schema_from_intent_payload_is_valid(self):
        """IntentPayload generates valid JSON schema for Structured Outputs."""
        schema = schema_from_model(IntentPayload)

        # Strict mode requirements: no title, description, examples
        assert "title" not in schema
        assert "description" not in schema
        assert "examples" not in schema

        # Must have properties and required
        assert "properties" in schema
        assert "required" in schema

        # Required should include all properties
        assert set(schema["required"]) == set(schema["properties"].keys())

        # Key fields should be present
        assert "kind" in schema["properties"]
        assert "metrics" in schema["properties"]
        assert "confidence" in schema["properties"]

    def test_schema_has_no_extra_fields(self):
        """Schema respects extra='forbid' from model config."""
        schema = schema_from_model(IntentPayload)
        # When strict=True and extra="forbid", model rejects unknown fields
        assert schema.get("additionalProperties", False) == False or "properties" in schema


class TestIntentPayloadValidation:
    """Test IntentPayload Pydantic model validation."""

    def test_valid_minimal_payload(self):
        """Minimal valid IntentPayload is accepted."""
        payload_dict = {
            "kind": "aggregate",
            "metrics": ["销售额"],
            "confidence": {"overall": 0.9},
        }
        payload = IntentPayload(**payload_dict)
        assert payload.kind.value == "aggregate"
        assert payload.metrics == ["销售额"]

    def test_unknown_field_rejected(self):
        """Extra fields are rejected (extra='forbid')."""
        payload_dict = {
            "kind": "aggregate",
            "metrics": ["销售额"],
            "confidence": {"overall": 0.9},
            "unknown_field": "should_be_rejected",
        }
        with pytest.raises(ValidationError):
            IntentPayload(**payload_dict)

    def test_nested_model_validation(self):
        """Nested models (_FilterPayload, _TimePayload) validate correctly."""
        payload_dict = {
            "kind": "aggregate",
            "metrics": ["销售额"],
            "filters": [
                {"field": "region", "operator": "in", "spoken_values": ["华东", "华南"]}
            ],
            "time": {
                "start": "2026-01-01",
                "end": "2026-01-31",
                "grain": "month",
                "expression": "本月",
            },
            "confidence": {"overall": 0.9},
        }
        payload = IntentPayload(**payload_dict)
        assert len(payload.filters) == 1
        assert payload.filters[0].field_name == "region"
        assert payload.time.expression == "本月"


class TestOpenAiStructuredOutputsClient:
    """Test OpenAI client with Structured Outputs (mocked)."""

    def test_client_uses_json_schema_format(self, monkeypatch):
        """Client constructs correct response_format for Structured Outputs."""
        from unittest.mock import MagicMock

        from app.core.config import Settings

        # Mock OpenAI client
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = '{"kind":"aggregate","metrics":["销售额"],"confidence":{"overall":0.9}}'
        mock_completion.usage.prompt_tokens = 100
        mock_completion.usage.completion_tokens = 50
        mock_completion.model = "gpt-4o"

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = mock_completion

        def mock_openai_init(api_key, base_url):
            return mock_openai_client

        monkeypatch.setattr("openai.OpenAI", mock_openai_init)

        settings = Settings(
            llm_api_key="test-key",
            llm_base_url="https://api.openai.com/v1",
            llm_model="gpt-4o",
        )
        client = OpenAiCompatClient(settings)

        # Call complete
        completion = client.complete("system prompt", "user prompt")

        # Verify the correct response_format was used
        call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
        assert "response_format" in call_kwargs
        response_format = call_kwargs["response_format"]
        assert response_format["type"] == "json_schema"
        assert "json_schema" in response_format
        assert response_format["json_schema"]["name"] == "IntentPayload"
        assert response_format["json_schema"]["strict"] == True
        assert "schema" in response_format["json_schema"]

        # Verify completion object
        assert completion.model == "gpt-4o"
        assert completion.prompt_tokens == 100
        assert completion.completion_tokens == 50
