from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.semantic.model import DatasetDef, EnumValueDef, FieldDef, MetricDef, SemanticError
from app.semantic.orm import DatasetRow, FieldRow, MetricRow


class UnknownDatasetError(SemanticError):
    pass


def _to_field(row: FieldRow) -> FieldDef:
    return FieldDef(
        name=row.name,
        physical_column=row.physical_column,
        semantic_type=row.semantic_type,
        business_name=row.business_name,
        synonyms=tuple(row.synonyms or ()),
        unit=row.unit,
        display_format=row.display_format,
        default_aggregation=row.default_aggregation,
        allowed_aggregations=tuple(row.allowed_aggregations or ()),
        is_filterable=row.is_filterable,
        is_groupable=row.is_groupable,
        is_queryable=row.is_queryable,
        sensitivity=row.sensitivity,
        enum_values=tuple(
            EnumValueDef(
                physical_value=value.physical_value,
                business_value=value.business_value,
                aliases=tuple(value.aliases or ()),
                description=value.description,
            )
            for value in row.enum_values
        ),
    )


def _to_metric(row: MetricRow) -> MetricDef:
    return MetricDef(
        name=row.name,
        business_name=row.business_name,
        kind=row.kind,
        time_field=row.time_field,
        version=row.version,
        synonyms=tuple(row.synonyms or ()),
        aggregation_behavior=row.aggregation_behavior,
        source_field=row.source_field,
        aggregation=row.aggregation,
        fixed_filter=row.fixed_filter,
        expression=row.expression,
        unit=row.unit,
        display_format=row.display_format,
        description=row.description,
    )


def _to_dataset(row: DatasetRow) -> DatasetDef:
    return DatasetDef(
        name=row.name,
        business_name=row.business_name,
        physical_table=row.physical_table,
        fields=tuple(_to_field(item) for item in row.fields),
        metrics=tuple(_to_metric(item) for item in row.metrics),
        aliases=tuple(row.aliases or ()),
        description=row.description,
        grain=row.grain,
        applicable_scenario=row.applicable_scenario,
        forbidden_scenario=row.forbidden_scenario,
        is_published=row.is_published,
        updated_at=row.updated_at,
    )


def _base_query():
    return select(DatasetRow).options(
        selectinload(DatasetRow.fields).selectinload(FieldRow.enum_values),
        selectinload(DatasetRow.metrics),
    )


def load_dataset(session: Session, name: str) -> DatasetDef:
    row = session.execute(_base_query().where(DatasetRow.name == name)).scalar_one_or_none()
    if row is None:
        raise UnknownDatasetError(name)
    return _to_dataset(row)


def list_datasets(session: Session) -> list[DatasetDef]:
    rows = session.execute(_base_query().order_by(DatasetRow.name)).scalars().all()
    return [_to_dataset(row) for row in rows]
