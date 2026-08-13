"""Shared semantic configuration matching sample.orders."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.semantic.enums import (
    Aggregation,
    AggregationBehavior,
    MetricKind,
    SemanticType,
    Sensitivity,
)
from app.semantic.orm import DatasetRow, EnumValueRow, FieldRow, MetricRow

_NO_AGG: list[str] = []
_NUMERIC_AGGS = [
    Aggregation.SUM.value,
    Aggregation.AVG.value,
    Aggregation.MAX.value,
    Aggregation.MIN.value,
]


def build_orders_dataset(session: Session, *, published: bool = True) -> DatasetRow:
    """Idempotent: if a dataset with this name already exists, reuse it.

    Earlier debugging left committed rows outside the test transaction's
    rollback boundary; without this guard every test would hit UniqueViolation.
    The published flag is reconciled so a caller asking for ``published=False``
    after the table was already populated with ``published=True`` still gets an
    unpublished dataset for that test.
    """
    existing = session.execute(
        select(DatasetRow).where(DatasetRow.name == "orders")
    ).scalar_one_or_none()
    if existing is not None:
        if existing.is_published != published:
            existing.is_published = published
            session.flush()
        return existing
    dataset = DatasetRow(
        name="orders",
        business_name="订单",
        physical_table="sample.orders",
        aliases=["订单表", "销售订单"],
        description="订单粒度的销售明细",
        grain="一行一个订单",
        applicable_scenario="销售额、订单量、毛利分析",
        forbidden_scenario="不可用于财务确认收入口径",
        is_published=published,
    )

    dataset.fields = [
        FieldRow(
            name="order_id",
            physical_column="order_id",
            business_name="订单ID",
            semantic_type=SemanticType.ID.value,
            default_aggregation=Aggregation.NONE.value,
            allowed_aggregations=[Aggregation.COUNT.value, Aggregation.DISTINCT_COUNT.value],
            is_groupable=False,
        ),
        FieldRow(
            name="order_no",
            physical_column="order_no",
            business_name="订单编号",
            semantic_type=SemanticType.ID.value,
            allowed_aggregations=[Aggregation.DISTINCT_COUNT.value],
            is_groupable=False,
        ),
        FieldRow(
            name="customer_id",
            physical_column="customer_id",
            business_name="客户ID",
            semantic_type=SemanticType.ID.value,
            allowed_aggregations=[Aggregation.DISTINCT_COUNT.value],
            is_groupable=False,
        ),
        FieldRow(
            name="customer_name",
            physical_column="customer_name",
            business_name="客户名称",
            semantic_type=SemanticType.TEXT.value,
            allowed_aggregations=_NO_AGG,
            sensitivity=Sensitivity.SENSITIVE.value,
        ),
        FieldRow(
            name="region_code",
            physical_column="region_code",
            business_name="大区",
            synonyms=["区域", "地区"],
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="EC", business_value="华东", aliases=["华东地区", "东区"]),
                EnumValueRow(physical_value="SC", business_value="华南", aliases=["华南地区", "南区"]),
                EnumValueRow(physical_value="NC", business_value="华北", aliases=["华北地区", "北区"]),
            ],
        ),
        FieldRow(
            name="province",
            physical_column="province",
            business_name="省份",
            semantic_type=SemanticType.TEXT.value,
            allowed_aggregations=_NO_AGG,
        ),
        FieldRow(
            name="channel",
            physical_column="channel",
            business_name="渠道",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="online", business_value="线上", aliases=["电商"]),
                EnumValueRow(physical_value="offline", business_value="线下", aliases=["门店"]),
            ],
        ),
        FieldRow(
            name="amount",
            physical_column="amount",
            business_name="订单金额",
            synonyms=["金额"],
            semantic_type=SemanticType.AMOUNT.value,
            unit="元",
            display_format="#,##0.00",
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="cost",
            physical_column="cost",
            business_name="订单成本",
            semantic_type=SemanticType.AMOUNT.value,
            unit="元",
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="quantity",
            physical_column="quantity",
            business_name="数量",
            semantic_type=SemanticType.QUANTITY.value,
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="is_new_customer",
            physical_column="is_new_customer",
            business_name="是否新客",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="true", business_value="新客"),
                EnumValueRow(physical_value="false", business_value="老客"),
            ],
        ),
        FieldRow(
            name="status",
            physical_column="status",
            business_name="订单状态",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="completed", business_value="已完成"),
                EnumValueRow(physical_value="cancelled", business_value="已取消"),
                EnumValueRow(physical_value="pending", business_value="待处理"),
            ],
        ),
        FieldRow(
            name="created_date",
            physical_column="created_date",
            business_name="下单日期",
            semantic_type=SemanticType.DATE.value,
            allowed_aggregations=[Aggregation.MAX.value, Aggregation.MIN.value],
        ),
        FieldRow(
            name="completed_date",
            physical_column="completed_date",
            business_name="完成日期",
            semantic_type=SemanticType.DATE.value,
            allowed_aggregations=[Aggregation.MAX.value, Aggregation.MIN.value],
        ),
    ]

    dataset.metrics = [
        MetricRow(
            name="sales_revenue",
            business_name="销售额",
            synonyms=["营收", "销售收入"],
            version=3,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="amount",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="元",
            display_format="#,##0.00",
            owner="sales-ops",
            description="已完成订单含税金额",
        ),
        MetricRow(
            name="order_count",
            business_name="订单量",
            synonyms=["订单数"],
            version=1,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="order_id",
            aggregation=Aggregation.COUNT.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="单",
        ),
        MetricRow(
            name="new_customer_revenue",
            business_name="新客销售额",
            version=1,
            kind=MetricKind.DERIVED.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="amount",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed' AND is_new_customer = true",
            time_field="completed_date",
            unit="元",
        ),
        MetricRow(
            name="total_cost",
            business_name="总成本",
            version=1,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="cost",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="元",
        ),
        MetricRow(
            name="gross_margin_rate",
            business_name="毛利率",
            synonyms=["毛利"],
            version=2,
            kind=MetricKind.RATIO.value,
            # Spec 4.4: ratio metrics must be recalculated, never summed.
            aggregation_behavior=AggregationBehavior.RECALCULATE.value,
            expression="(sales_revenue - total_cost) / sales_revenue",
            time_field="completed_date",
            display_format="0.00%",
            description="(销售额 - 总成本) / 销售额",
        ),
    ]

    session.add(dataset)
    session.flush()
    return dataset
