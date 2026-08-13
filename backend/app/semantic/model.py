from dataclasses import dataclass, field as dc_field
from datetime import datetime


class SemanticError(Exception):
    """Base class for semantic-layer lookup failures."""


class UnknownFieldError(SemanticError):
    pass


class UnknownMetricError(SemanticError):
    pass


@dataclass(frozen=True, slots=True)
class EnumValueDef:
    physical_value: str
    business_value: str
    aliases: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class FieldDef:
    name: str
    physical_column: str
    semantic_type: str
    business_name: str = ""
    synonyms: tuple[str, ...] = ()
    unit: str = ""
    display_format: str = ""
    default_aggregation: str = "none"
    allowed_aggregations: tuple[str, ...] = ()
    is_filterable: bool = True
    is_groupable: bool = True
    is_queryable: bool = True
    sensitivity: str = "public"
    enum_values: tuple[EnumValueDef, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricDef:
    name: str
    business_name: str
    kind: str
    time_field: str
    version: int = 1
    synonyms: tuple[str, ...] = ()
    aggregation_behavior: str = "additive"
    source_field: str | None = None
    aggregation: str | None = None
    fixed_filter: str = ""
    expression: str = ""
    unit: str = ""
    display_format: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class DatasetDef:
    name: str
    business_name: str
    physical_table: str
    fields: tuple[FieldDef, ...]
    metrics: tuple[MetricDef, ...]
    aliases: tuple[str, ...] = ()
    description: str = ""
    grain: str = ""
    applicable_scenario: str = ""
    forbidden_scenario: str = ""
    is_published: bool = False
    updated_at: datetime | None = None

    def field(self, name: str) -> FieldDef:
        for item in self.fields:
            if item.name == name:
                return item
        raise UnknownFieldError(name)

    def metric(self, name: str) -> MetricDef:
        for item in self.metrics:
            if item.name == name:
                return item
        raise UnknownMetricError(name)

    def has_field(self, name: str) -> bool:
        return any(item.name == name for item in self.fields)

    def has_metric(self, name: str) -> bool:
        return any(item.name == name for item in self.metrics)

    def resolve_enum(self, field_name: str, spoken: str) -> str | None:
        """Map a spoken business value or alias to its physical value.

        Returns None when nothing matches; callers must raise clarification
        rather than silently querying an unmapped value (spec 4.3).
        """
        target = spoken.strip().casefold()
        for value in self.field(field_name).enum_values:
            candidates = (value.business_value, value.physical_value, *value.aliases)
            if any(candidate.casefold() == target for candidate in candidates):
                return value.physical_value
        return None
