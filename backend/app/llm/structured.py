"""Structured Outputs with Pydantic schema -> JSON Schema conversion."""

from __future__ import annotations

import json
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def schema_from_model(model: Type[T]) -> dict:
    """Convert Pydantic model to JSON Schema for Structured Outputs (strict mode).

    Removes title/description cruft that strict mode doesn't allow.
    """
    schema = model.model_json_schema()

    # Strict mode doesn't allow certain fields
    schema.pop("title", None)
    schema.pop("description", None)
    schema.pop("examples", None)

    # Ensure all properties are required (strict mode requirement)
    if "properties" in schema:
        schema["required"] = list(schema["properties"].keys())

    return schema


def validate_against_schema(output: str, schema: dict, model: Type[T]) -> T:
    """Parse and validate LLM output against the schema.

    Raises ValidationError if the output doesn't match.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        raise ValidationError.from_exception_data("json", []) from e

    return model.model_validate(data)
