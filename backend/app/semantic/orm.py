from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

META_SCHEMA = "agent_meta"


class Base(DeclarativeBase):
    metadata = MetaData(schema=META_SCHEMA)


class DatasetRow(Base):
    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    business_name: Mapped[str] = mapped_column(String(128))
    physical_table: Mapped[str] = mapped_column(String(256))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    grain: Mapped[str] = mapped_column(String(128), default="")
    applicable_scenario: Mapped[str] = mapped_column(Text, default="")
    forbidden_scenario: Mapped[str] = mapped_column(Text, default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    fields: Mapped[list["FieldRow"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )
    metrics: Mapped[list["MetricRow"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class FieldRow(Base):
    __tablename__ = "field"
    __table_args__ = (UniqueConstraint("dataset_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    physical_column: Mapped[str] = mapped_column(String(128))
    business_name: Mapped[str] = mapped_column(String(128), default="")
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    semantic_type: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(32), default="")
    display_format: Mapped[str] = mapped_column(String(32), default="")
    default_aggregation: Mapped[str] = mapped_column(String(16), default="none")
    allowed_aggregations: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_groupable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_queryable: Mapped[bool] = mapped_column(Boolean, default=True)
    sensitivity: Mapped[str] = mapped_column(String(16), default="public")

    # Reserved for the next iteration (spec 4.2): stored but never read by the
    # compiler and not exposed in the config UI.
    business_object: Mapped[str] = mapped_column(String(128), default="")
    pii_level: Mapped[str] = mapped_column(String(16), default="")
    default_recall: Mapped[bool] = mapped_column(Boolean, default=False)
    value_recall: Mapped[bool] = mapped_column(Boolean, default=False)

    dataset: Mapped[DatasetRow] = relationship(back_populates="fields")
    enum_values: Mapped[list["EnumValueRow"]] = relationship(
        back_populates="field", cascade="all, delete-orphan"
    )


class EnumValueRow(Base):
    __tablename__ = "enum_value"
    __table_args__ = (UniqueConstraint("field_id", "physical_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("field.id", ondelete="CASCADE"))
    physical_value: Mapped[str] = mapped_column(String(128))
    business_value: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    description: Mapped[str] = mapped_column(Text, default="")

    field: Mapped[FieldRow] = relationship(back_populates="enum_values")


class MetricRow(Base):
    __tablename__ = "metric"
    __table_args__ = (UniqueConstraint("dataset_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    business_name: Mapped[str] = mapped_column(String(128))
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16))
    aggregation_behavior: Mapped[str] = mapped_column(String(16), default="additive")

    # ATOMIC: aggregation over source_field. DERIVED: same plus fixed_filter.
    # COMPOSITE / RATIO: expression referencing other metric names.
    source_field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fixed_filter: Mapped[str] = mapped_column(Text, default="")
    expression: Mapped[str] = mapped_column(Text, default="")

    # Spec 4.4: the date column this metric is measured on. Not nullable —
    # an unstated time basis is the usual cause of one metric yielding two numbers.
    time_field: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="")
    display_format: Mapped[str] = mapped_column(String(32), default="")
    owner: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    dataset: Mapped[DatasetRow] = relationship(back_populates="metrics")