from __future__ import annotations

import pandas as pd

from backend.services.results.rendering import _charts, _kpis, _report_recommendations


def test_result_cards_show_only_query_values_and_charts_follow_data_shape():
    grouped = pd.DataFrame({"区域": ["北区", "南区"], "销售额": [120, 90]})
    assert _kpis(grouped) == []
    assert [item["type"] for item in _charts(grouped)] == ["bar", "pie"]

    trend = pd.DataFrame({"月份": ["2026-01", "2026-02"], "销售额": [120, 150]})
    assert [item["type"] for item in _charts(trend)] == ["line"]

    single = pd.DataFrame({"销售额": [210], "订单数": [3]})
    assert [(item["label"], item["value"]) for item in _kpis(single)] == [
        ("销售额", 210), ("订单数", 3),
    ]


def test_report_recommendations_require_explicit_text():
    assert _report_recommendations("分析完成，销售额有所变化。") == {
        "short_term": [], "medium_term": [], "long_term": [],
    }
    assert _report_recommendations("短期建议：复核异常订单\n- 检查退货口径\n长期：持续监测") == {
        "short_term": ["复核异常订单", "检查退货口径"],
        "medium_term": [], "long_term": ["持续监测"],
    }
