"""Deterministic demo dataset for 数擎 Data Agent.

The bundled sample must let a first-time user ask real questions and get real,
non-trivial answers.  That means a *multi-dimensional sales fact table* with
enough history for year-over-year comparison and forecasting — not a static
one-sheet lookup table.

The generator is seeded, so the same anchor month produces identical data.
The default anchor is the current month; new workspaces seeded in different
months therefore have different time windows and numeric answers.

The narrative baked into the data is deliberate:

* 华东 (上海/杭州/南京/苏州) grows for ~20 months, then 家电 falls sharply in the
  last three months while 苏州 also loses its 企业团购 volume.  That is what
  makes "为什么华东销售下降" answerable rather than hand-wavy.
* 深圳 has one month where 生鲜 spikes far beyond its own history, which is what
  "哪些商品表现异常" should surface.
* 客单价 falls as a *consequence* of the 家电 mix shift, so a 量价拆解 produces a
  real answer instead of two unrelated numbers.
"""

from __future__ import annotations

import io
import math
import random
from datetime import date
from typing import Any, Iterator

import pandas as pd

SAMPLE_SEED_ID = "retail_sales_monthly"
MONTHS = 24

REGIONS: dict[str, dict[str, Any]] = {
    "华东": {"provinces": {"上海": "上海", "杭州": "浙江", "南京": "江苏", "苏州": "江苏"},
             "base": 4_200_000, "growth": 0.002, "season": 0.16},
    "华北": {"provinces": {"北京": "北京", "天津": "天津"},
             "base": 2_600_000, "growth": 0.007, "season": 0.10},
    "华南": {"provinces": {"深圳": "广东", "广州": "广东"},
             "base": 2_900_000, "growth": 0.013, "season": 0.12},
    "华中": {"provinces": {"武汉": "湖北", "长沙": "湖南"},
             "base": 1_800_000, "growth": 0.009, "season": 0.09},
    "西南": {"provinces": {"成都": "四川", "重庆": "重庆"},
             "base": 1_700_000, "growth": 0.010, "season": 0.11},
}

# 品类 -> (share of regional revenue, average order value, order-count weight)
CATEGORIES: dict[str, tuple[float, float, float]] = {
    "生鲜": (0.28, 96, 4.6),
    "食品饮料": (0.24, 132, 2.8),
    "美妆个护": (0.19, 268, 1.1),
    "家电": (0.17, 2_150, 0.10),
    "母婴": (0.12, 342, 0.72),
}

CHANNELS: dict[str, float] = {"即时配送": 0.46, "门店自提": 0.34, "企业团购": 0.20}

# City share within its region, and how strongly that city leans on 企业团购.
CITY_PROFILE: dict[str, tuple[float, float]] = {
    "上海": (0.32, 0.10), "杭州": (0.26, 0.14), "南京": (0.24, 0.18), "苏州": (0.18, 0.46),
    "北京": (0.62, 0.18), "天津": (0.38, 0.12),
    "深圳": (0.58, 0.12), "广州": (0.42, 0.26),
    "武汉": (0.55, 0.20), "长沙": (0.45, 0.15),
    "成都": (0.57, 0.14), "重庆": (0.43, 0.22),
}

COLUMNS = (
    "统计月份", "统计年月", "区域", "省份", "城市", "品类", "渠道",
    "销售额", "订单量", "客户数", "折扣金额", "退款金额",
)


def _month_starts(anchor: date, count: int) -> list[date]:
    """`count` consecutive month starts ending with `anchor`'s month."""
    year, month = anchor.year, anchor.month
    months: list[date] = []
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def _seasonality(month: int) -> float:
    """Retail seasonality: a February trough, a Q4 peak, a summer bump."""
    factors = {
        1: -0.06, 2: -0.22, 3: -0.04, 4: 0.03, 5: 0.05, 6: 0.09,
        7: 0.06, 8: 0.04, 9: 0.01, 10: 0.08, 11: 0.14, 12: 0.18,
    }
    return factors.get(month, 0.0)


def _east_home_appliance_hit(months_from_end: int) -> float:
    """Multiplier for 华东 × 家电 in the closing months of the series.

    ``0`` is the most recent month.  The collapse deepens as it approaches, so
    "为什么华东销售下降" has a date to point at rather than a vague trend.
    """
    if months_from_end == 0:
        return 0.46
    if months_from_end == 1:
        return 0.60
    if months_from_end == 2:
        return 0.76
    if months_from_end == 3:
        return 0.88
    return 1.0


def _shenzhen_fresh_anomaly(months_from_end: int) -> float:
    """One deliberate spike so 异常识别 has something real to find."""
    return 2.85 if months_from_end == 2 else 1.0


def _rows(anchor: date) -> Iterator[dict[str, Any]]:
    # Deterministic pseudo-randomness: the same anchor month produces
    # identical numbers across installs.
    rng = random.Random(20260917)  # noqa: S311
    months = _month_starts(anchor, MONTHS)
    for index, month in enumerate(months):
        months_from_end = len(months) - 1 - index
        season = _seasonality(month.month)
        for region, profile in REGIONS.items():
            for city, (city_share, corp_share) in CITY_PROFILE.items():
                if city not in profile["provinces"]:
                    continue
                region_total = (
                    profile["base"]
                    * city_share
                    * ((1 + profile["growth"]) ** index)
                    * (1 + season)
                    * (1 + rng.uniform(-0.035, 0.035))
                )
                for category, (category_share, avg_value, _weight) in CATEGORIES.items():
                    if city == "广州" and category == "食品饮料" and months_from_end < 5:
                        category_share *= 0.82  # a slow, believable slide
                    revenue = region_total * category_share * (1 + rng.uniform(-0.05, 0.05))
                    if region == "华东" and category == "家电":
                        revenue *= _east_home_appliance_hit(months_from_end)
                    if city == "深圳" and category == "生鲜":
                        revenue *= _shenzhen_fresh_anomaly(months_from_end)
                    for channel, channel_share in CHANNELS.items():
                        share = channel_share
                        if city == "苏州" and channel == "企业团购" and months_from_end < 3:
                            # 苏州 lost its largest corporate account in the last quarter.
                            share *= 0.34
                        channel_revenue = revenue * share
                        if channel_revenue < 1:
                            continue
                        value = avg_value * (1 + rng.uniform(-0.08, 0.08))
                        orders = max(1, int(round(channel_revenue / value)))
                        discount = channel_revenue * (
                            0.055 if channel == "企业团购" else 0.085
                        ) * (1 + rng.uniform(-0.25, 0.35))
                        if region == "华东" and category == "家电" and months_from_end < 4:
                            discount *= 1.9  # clearance pulled 客单价 down further
                        customers = max(1, int(round(orders / rng.uniform(1.35, 1.95))))
                        refund = channel_revenue * (
                            0.018 if category == "生鲜" else 0.006
                        ) * (1 + rng.uniform(-0.4, 0.6))
                        yield {
                            "统计月份": month.isoformat(),
                            "统计年月": f"{month.year:04d}-{month.month:02d}",
                            "区域": region,
                            "省份": profile["provinces"][city],
                            "城市": city,
                            "品类": category,
                            "渠道": channel,
                            "销售额": round(channel_revenue, 2),
                            "订单量": orders,
                            "客户数": customers,
                            "折扣金额": round(discount, 2),
                            "退款金额": round(refund, 2),
                        }


def build_frame(anchor: date | None = None) -> pd.DataFrame:
    """Build the demo fact table.  Deterministic for a given anchor month."""
    anchor = anchor or date.today().replace(day=1)
    frame = pd.DataFrame(list(_rows(anchor)), columns=list(COLUMNS))
    return frame.sort_values(["统计月份", "区域", "城市", "品类", "渠道"]).reset_index(drop=True)


def to_csv_bytes(frame: pd.DataFrame | None = None) -> bytes:
    buffer = io.StringIO()
    (frame if frame is not None else build_frame()).to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def sample_questions() -> list[str]:
    """The questions the demo is expected to answer with real numbers."""
    return [
        "本月销售额是多少？",
        "华东销售同比怎么样？",
        "哪个城市下降最多？",
        "为什么华东销售下降？",
        "哪些商品表现异常？",
        "预测下个月销售额。",
        "生成经营分析报告。",
        "生成经营分析 PPT。",
    ]


def semantic_model_payload(table: str) -> dict[str, Any]:
    return {
        "name": "销售月度事实模型",
        "description": "按月 × 区域 × 城市 × 品类 × 渠道组织的销售事实，覆盖演示所需的同比、归因与预测场景。",
        "table": table,
        "grain": "月 × 区域 × 城市 × 品类 × 渠道",
        "entities": [{"name": "城市", "column": "城市", "type": "primary", "label": "城市"}],
        "default_time_dimension": "统计月份",
        "dimensions": [
            {"name": "统计月份", "column": "统计月份", "type": "time", "label": "统计月份"},
            {"name": "统计年月", "column": "统计年月", "type": "time", "label": "统计年月"},
            {"name": "区域", "column": "区域", "type": "categorical", "label": "区域"},
            {"name": "省份", "column": "省份", "type": "categorical", "label": "省份"},
            {"name": "城市", "column": "城市", "type": "categorical", "label": "城市"},
            {"name": "品类", "column": "品类", "type": "categorical", "label": "品类"},
            {"name": "渠道", "column": "渠道", "type": "categorical", "label": "渠道"},
        ],
        "measures": [
            {"name": "sales_amount", "column": "销售额", "aggregation": "sum", "label": "销售额"},
            {"name": "order_count", "column": "订单量", "aggregation": "sum", "label": "订单量"},
            {"name": "customer_count", "column": "客户数", "aggregation": "sum", "label": "客户数"},
            {"name": "discount_amount", "column": "折扣金额", "aggregation": "sum", "label": "折扣金额"},
            {"name": "refund_amount", "column": "退款金额", "aggregation": "sum", "label": "退款金额"},
        ],
    }


def metric_payloads() -> list[dict[str, Any]]:
    """Formal metrics the demo publishes.  Names double as Agent metric hints."""
    return [
        {
            "name": "sales_amount", "label": "销售额", "metric_type": "atomic", "measure": "sales_amount",
            "description": "统计期内各渠道订单的成交金额合计，未扣除退款。", "unit": "元",
            "format": ",.2f", "aliases": ["GMV", "成交额", "营收", "销售收入"], "status": "approved",
            "business_object": "订单", "business_event": "支付成功",
            "grain": "月 × 区域 × 城市 × 品类 × 渠道",
            "time_semantics": "按订单支付完成月份归集，自然月",
            "deduplication": "事实表已在生成阶段按维度组合汇总，无需去重",
        },
        {
            "name": "order_count", "label": "订单量", "metric_type": "atomic", "measure": "order_count",
            "description": "统计期内成交订单的笔数。", "unit": "单", "format": ",.0f",
            "aliases": ["单量", "订单数", "成交笔数"], "status": "approved",
            "business_object": "订单", "business_event": "支付成功",
            "grain": "月 × 区域 × 城市 × 品类 × 渠道",
            "time_semantics": "按订单支付完成月份归集",
        },
        {
            "name": "customer_count", "label": "客户数", "metric_type": "atomic", "measure": "customer_count",
            "description": "统计期内产生成交的客户数。", "unit": "人", "format": ",.0f",
            "aliases": ["买家数", "成交客户数"], "status": "approved",
            "business_object": "客户", "business_event": "支付成功",
        },
        {
            "name": "refund_amount", "label": "退款金额", "metric_type": "atomic", "measure": "refund_amount",
            "description": "统计期内发生退款的金额。生鲜类退款率显著高于其他品类。", "unit": "元",
            "format": ",.2f", "aliases": ["退货金额", "退款额"], "status": "approved",
            "business_object": "订单", "business_event": "退款完成",
        },
        {
            "name": "discount_amount", "label": "折扣金额", "metric_type": "atomic", "measure": "discount_amount",
            "description": "统计期内为促销让利而减免的金额。", "unit": "元", "format": ",.2f",
            "aliases": ["促销投入", "优惠金额"], "status": "approved",
            "business_object": "订单", "business_event": "促销让利",
        },
        {
            "name": "net_sales_amount", "label": "净销售额", "metric_type": "derived",
            "expression": "sales_amount - refund_amount",
            "description": "成交金额扣除退款后的净额。", "unit": "元", "format": ",.2f",
            "aliases": ["净营收", "净成交额"], "status": "approved",
            "business_object": "订单", "business_event": "支付成功并扣除退款",
        },
        {
            "name": "average_order_value", "label": "客单价", "metric_type": "derived",
            "expression": "sales_amount / order_count",
            "description": "每笔订单的平均成交金额。", "unit": "元", "format": ",.2f",
            "aliases": ["客单", "单均价", "AOV"], "status": "approved",
            "business_object": "订单", "business_event": "支付成功",
            "time_semantics": "按支付完成月份归集，先汇总再相除，不对单笔订单求平均",
        },
        {
            "name": "discount_rate", "label": "折扣率", "metric_type": "derived",
            "expression": "discount_amount / (sales_amount + discount_amount)",
            "description": "折扣金额占原价销售额的比例，反映促销力度。", "unit": "", "format": ".2%",
            "aliases": ["促销力度", "打折率"], "status": "approved",
        },
    ]


def knowledge_payloads() -> list[dict[str, Any]]:
    return [
        {
            "key": "sales_metric_definition", "type": "metric", "name": "销售额口径",
            "alias": "GMV, 成交额",
            "definition": (
                "销售额指统计期内各渠道成交订单的支付金额合计，不扣退款；需要净额时使用净销售额。"
                "客单价必须先汇总销售额与订单量再相除，不能对单笔订单取平均。"
            ),
            "notes": "演示数据的正式口径，Agent 回答经营问题时必须遵循。",
        },
        {
            "key": "east_china_context", "type": "context_note", "name": "华东经营背景",
            "topic": "华东经营背景",
            "content": (
                "华东含上海、杭州、南京、苏州四城。家电是华东客单价最高的品类，"
                "家电占比变化会显著拉动整体客单价；苏州的企业团购占比高于其他城市，"
                "大客户流失会同时影响销售额与订单量。"
            ),
            "tags": ["demo", "华东", "口径"],
        },
        {
            "key": "sales_anomaly_rule", "type": "business_rule", "name": "异常值判断规则",
            "rule_id": "SALES-ANOMALY-001",
            "description": (
                "当某品类某月的销售额偏离该品类近 12 个月均值超过 2 倍标准差时标记为异常，"
                "并要求同时给出异常前后的订单量与客单价变化，以区分真实增长与数据问题。"
            ),
            "severity": "medium",
        },
        {
            "key": "forecast_rule", "type": "business_rule", "name": "预测口径规则",
            "rule_id": "SALES-FORECAST-001",
            "description": (
                "预测必须基于至少 12 个完整自然月；同时输出预测区间与关键假设；"
                "月份不足时只给趋势方向并说明数据不足，不得输出精确预测值。"
            ),
            "severity": "high",
        },
    ]


def summary(frame: pd.DataFrame) -> dict[str, Any]:
    """Facts a UI can show without recomputing anything."""
    latest = frame["统计年月"].max()
    month_rows = frame[frame["统计年月"] == latest]
    return {
        "months": int(frame["统计年月"].nunique()),
        "rows": int(len(frame)),
        "latest_month": latest,
        "latest_sales_amount": round(float(month_rows["销售额"].sum()), 2),
        "regions": sorted(frame["区域"].unique().tolist()),
        "cities": sorted(frame["城市"].unique().tolist()),
        "categories": sorted(frame["品类"].unique().tolist()),
        "channels": sorted(frame["渠道"].unique().tolist()),
    }


def is_finite(value: float) -> bool:
    return math.isfinite(value)
