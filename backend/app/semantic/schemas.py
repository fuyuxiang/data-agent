from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EnumValueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    physical_value: str
    business_value: str
    aliases: list[str]
    description: str


class FieldOut(BaseModel):
    """Admin view of a field — includes physical column, sensitivity, and
    aggregation controls. Use `FieldOutPublic` for callers that should not
    see physical implementation details.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    physical_column: str
    business_name: str
    synonyms: list[str]
    semantic_type: str
    unit: str
    display_format: str
    default_aggregation: str
    allowed_aggregations: list[str]
    is_filterable: bool
    is_groupable: bool
    is_queryable: bool
    sensitivity: str
    enum_values: list[EnumValueOut]


class FieldOutPublic(BaseModel):
    """Business view of a field — what end-users see in the workbench."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    synonyms: list[str]
    semantic_type: str
    unit: str
    display_format: str
    enum_values: list[EnumValueOut]


class MetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    synonyms: list[str]
    version: int
    kind: str
    aggregation_behavior: str
    source_field: str | None
    aggregation: str | None
    fixed_filter: str
    expression: str
    time_field: str
    unit: str
    display_format: str
    description: str


class DatasetSummaryOut(BaseModel):
    """Admin summary — includes physical_table for ownership / debugging."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    physical_table: str
    grain: str
    is_published: bool
    updated_at: datetime | None


class DatasetSummaryOutPublic(BaseModel):
    """Business summary — no physical implementation details."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    is_published: bool
    updated_at: datetime | None


class DatasetDetailOut(DatasetSummaryOut):
    aliases: list[str]
    description: str
    applicable_scenario: str
    forbidden_scenario: str
    fields: list[FieldOut]
    metrics: list[MetricOut]


class DatasetDetailOutPublic(BaseModel):
    """Business detail — what end-users see. Excludes physical_table, grain,
    and physical_column/sensitivity on fields."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    aliases: list[str]
    description: str
    applicable_scenario: str
    forbidden_scenario: str
    is_published: bool
    updated_at: datetime | None
    fields: list[FieldOutPublic]
    metrics: list[MetricOut]


class LintIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    severity: str
    target: str
    message: str


class LintReportOut(BaseModel):
    dataset: str
    publishable: bool
    issues: list[LintIssueOut]


class PublishResultOut(BaseModel):
    dataset: str
    published: bool
    issues: list[LintIssueOut]