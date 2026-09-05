from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .datasets import frame_records


CHART_CATALOG = [
    ("bar", "条形图", "comparison"), ("grouped_bar", "分组条形图", "comparison"),
    ("stacked_bar", "堆叠条形图", "comparison"), ("diverging_bar", "发散条形图", "comparison"),
    ("bullet", "子弹图", "comparison"), ("dot", "点图", "comparison"),
    ("slope", "坡度图", "comparison"), ("waterfall", "瀑布图", "comparison"),
    ("line", "折线图", "trend"), ("area", "面积图", "trend"),
    ("stacked_area", "堆叠面积图", "trend"), ("sparkline", "迷你趋势图", "trend"),
    ("circular_line", "极坐标折线图", "trend"), ("bump", "排名变化图", "trend"),
    ("horizon", "地平线图", "trend"), ("cycle", "周期图", "trend"),
    ("connected_scatter", "连接散点图", "trend"), ("candlestick", "K 线图", "trend"),
    ("histogram", "直方图", "distribution"), ("pareto", "帕累托图", "distribution"),
    ("boxplot", "箱线图", "distribution"), ("violin", "小提琴图", "distribution"),
    ("ridgeline", "山峦图", "distribution"), ("beeswarm", "蜂群图", "distribution"),
    ("error_bar", "误差棒图", "distribution"), ("density", "密度图", "distribution"),
    ("scatter", "散点图", "relationship"), ("bubble", "气泡图", "relationship"),
    ("heatmap", "热力图", "relationship"), ("parallel", "平行坐标图", "relationship"),
    ("network", "关系网络图", "relationship"), ("chord", "弦图", "relationship"),
    ("sankey", "桑基图", "relationship"), ("radar", "雷达图", "relationship"),
    ("pie", "饼图", "composition"), ("donut", "环形图", "composition"),
    ("rose", "南丁格尔玫瑰图", "composition"), ("treemap", "矩形树图", "composition"),
    ("sunburst", "旭日图", "composition"), ("waffle", "华夫图", "composition"),
    ("marimekko", "马赛克图", "composition"), ("funnel", "漏斗图", "composition"),
    ("pyramid", "金字塔图", "composition"), ("gauge", "仪表盘", "indicator"),
    ("choropleth", "分级统计地图", "geography"), ("dot_map", "点密度地图", "geography"),
    ("calendar", "日历热力图", "geography"),
]

# Stable chart IDs and field-role contracts accepted by the public API.
# Rendering remains native ECharts JSON, so saved charts and dashboards do not
# depend on generated Python/HTML files.
CHART_TYPE_CONTRACTS: dict[str, tuple[str, list[str], list[str]]] = {
    "Marimekko_ABS": ("marimekko", ["x", "y", "group"], []),
    "Marimekko_PCT": ("marimekko", ["x", "y", "group"], []),
    "Bar_Chart": ("bar", ["x", "y"], ["series", "color"]),
    "Grouped_Bar_Chart": ("grouped_bar", ["x", "y", "series"], ["color", "value_cols"]),
    "Stacked_Bar_Chart": ("stacked_bar", ["x", "y", "series"], ["color", "value_cols"]),
    "Diverging_Bar_Chart": ("diverging_bar", ["label", "value"], []),
    "Dot_Plot": ("dot", ["category", "start", "end"], []),
    "Waffle": ("waffle", ["category", "value"], []),
    "Bullet_Chart": ("bullet", ["label", "actual", "target"], ["low", "medium", "high"]),
    "Sankey_Chart": ("sankey", ["source", "target", "value"], []),
    "Heatmap": ("heatmap", ["x", "y", "value"], []),
    "Waterfall": ("waterfall", ["x", "y"], ["type"]),
    "Line_Chart": ("line", ["x", "y"], ["series"]),
    "Circular_Line_Chart": ("circular_line", ["x", "y"], ["series"]),
    "Slope_Chart": ("slope", ["group", "start", "end"], []),
    "Sparkline": ("sparkline", ["x", "y"], []),
    "Bump_Chart": ("bump", ["x", "y", "group"], ["highlight"]),
    "Cycle_Chart": ("cycle", ["time", "value"], []),
    "Area_Chart": ("area", ["x", "y"], ["series"]),
    "Stacked_Area_Chart": ("stacked_area", ["x", "y"], ["series"]),
    "Horizon_Chart": ("horizon", ["x", "y"], ["series"]),
    "Connected_Scatter": ("connected_scatter", ["x", "y"], ["order", "size"]),
    "Histogram_Pareto_chart": ("histogram", ["value"], []),
    "Pyramid_Chart": ("pyramid", ["label", "left_value", "right_value"], []),
    "Error_Bar_Chart": ("error_bar", ["label", "value"], []),
    "Box-and-Whisker_Plot": ("boxplot", ["y"], ["x"]),
    "Violin_Chart": ("violin", ["y"], ["x"]),
    "Ridgeline_Plot": ("ridgeline", ["group", "value"], []),
    "Beeswarm_Plot": ("beeswarm", ["y"], ["x"]),
    "Dot_Density_Map": ("dot_map", ["label", "value"], ["category"]),
    "Choropleth_Map": ("choropleth", ["label", "value"], []),
    "Scatter_Plot": ("scatter", ["x", "y"], ["size", "color"]),
    "Bubble_Plot": ("bubble", ["x", "y"], ["size", "color", "x_mid", "y_mid"]),
    "Chord_Diagram": ("chord", ["source", "target", "value"], []),
    "Arc_Chart": ("network", ["x", "y", "z"], []),
    "Network_Diagram": ("network", ["source", "target"], ["weight"]),
    "Parallel_Coordinates_Plot": ("parallel", ["dimensions"], ["color"]),
    "Treemap": ("treemap", ["labels", "values"], ["parents"]),
    "Sunburst_Diagram": ("sunburst", ["labels", "values"], ["parents"]),
    "Nightingale_Chart": ("rose", ["names", "values"], []),
    "Pie_Chart": ("pie", ["label", "value"], ["color"]),
}


_CHART_KEYWORDS = {
    "bar": ("柱", "条形", "bar", "对比", "比较"),
    "grouped_bar": ("分组柱", "并排", "grouped"),
    "stacked_bar": ("堆叠柱", "堆积", "stacked bar"),
    "diverging_bar": ("正负", "发散", "diverging", "支持与反对"),
    "dot": ("点图", "dot plot", "起止"),
    "bullet": ("kpi", "达成率", "目标", "bullet", "子弹图"),
    "sankey": ("桑基", "流向", "流量", "sankey"),
    "heatmap": ("热力", "热图", "相关矩阵", "heatmap"),
    "waterfall": ("瀑布", "增减拆解", "waterfall", "bridge chart"),
    "line": ("折线", "趋势", "时序", "line chart"),
    "circular_line": ("圆形折线", "极坐标", "circular"),
    "slope": ("斜率", "两个时间点", "slope"),
    "sparkline": ("迷你图", "sparkline"),
    "bump": ("排名变化", "凹凸", "bump"),
    "cycle": ("周期", "季节", "cycle"),
    "area": ("面积图", "area chart"),
    "stacked_area": ("堆叠面积", "stacked area"),
    "horizon": ("地平线", "horizon"),
    "connected_scatter": ("连线散点", "轨迹", "connected scatter"),
    "histogram": ("直方", "分布", "histogram"),
    "pareto": ("帕累托", "二八", "pareto", "80/20"),
    "pyramid": ("金字塔", "年龄结构", "pyramid"),
    "error_bar": ("误差", "置信区间", "error bar"),
    "boxplot": ("箱线", "四分位", "boxplot", "box plot"),
    "violin": ("小提琴", "violin"),
    "ridgeline": ("山脊", "ridgeline", "ridge plot"),
    "beeswarm": ("蜂群", "分簇散点", "beeswarm"),
    "choropleth": ("面量", "填充地图", "choropleth"),
    "dot_map": ("点密度", "dot density", "地理分布"),
    "scatter": ("散点", "相关性", "scatter"),
    "bubble": ("气泡", "象限", "bubble", "bcg"),
    "chord": ("弦图", "多向关系", "chord"),
    "network": ("网络", "节点", "关系图谱", "network", "arc chart"),
    "parallel": ("平行坐标", "多维变量", "parallel coordinates"),
    "treemap": ("矩形树", "treemap", "层级占比"),
    "sunburst": ("旭日", "sunburst"),
    "rose": ("玫瑰", "南丁格尔", "nightingale"),
    "pie": ("饼图", "占比", "构成", "pie chart"),
    "waffle": ("华夫", "方格占比", "waffle"),
    "marimekko": ("马里美科", "marimekko", "双维占比"),
}


def catalog() -> list[dict]:
    catalog_ids: dict[str, list[str]] = {}
    for catalog_id, (chart_id, _required, _optional) in CHART_TYPE_CONTRACTS.items():
        catalog_ids.setdefault(chart_id, []).append(catalog_id)
    return [
        {"id": chart_id, "name": name, "group": group, "catalog_ids": catalog_ids.get(chart_id, [])}
        for chart_id, name, group in CHART_CATALOG
    ]


def normalize_chart_type(requested: str | None, field_mapping: dict | None = None) -> str | None:
    value = str(requested or "").strip()
    if not value:
        return None
    if value == "Histogram_Pareto_chart" and field_mapping:
        return "pareto" if field_mapping.get("x") is not None and field_mapping.get("y") is not None else "histogram"
    if value in CHART_TYPE_CONTRACTS:
        return CHART_TYPE_CONTRACTS[value][0]
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        key.lower().replace("-", "_").replace(" ", "_"): chart_type
        for key, (chart_type, _required, _optional) in CHART_TYPE_CONTRACTS.items()
    }
    return aliases.get(normalized, normalized)


def select_charts(user_intent: str, available_columns: list[str] | None = None, top_n: int = 3) -> list[dict]:
    """Rank chart contracts without importing or executing generated chart code."""
    intent = str(user_intent or "").lower()
    columns = [str(value) for value in available_columns or []]
    column_text = " ".join(columns).lower()
    signals = {
        "bullet": (("actual", "target", "实际", "目标"), 2),
        "sankey": (("source", "target", "from", "to", "源", "目标节点"), 2),
        "pyramid": (("left", "right", "male", "female", "男", "女"), 2),
        "slope": (("start", "end", "before", "after", "期初", "期末"), 2),
    }
    scored = []
    name_by_id = {chart_id: (name, group) for chart_id, name, group in CHART_CATALOG}
    for catalog_id, (chart_type, required, optional) in CHART_TYPE_CONTRACTS.items():
        score = 0
        if catalog_id.lower() in intent or chart_type.replace("_", " ") in intent:
            score += 30
        score += 12 * sum(keyword in intent for keyword in _CHART_KEYWORDS.get(chart_type, ()))
        if chart_type in signals:
            terms, threshold = signals[chart_type]
            if sum(term in column_text for term in terms) >= threshold:
                score += 18
        for role in [*required, *optional]:
            if any(role.lower() in column.lower() for column in columns):
                score += 2
        if score:
            name, group = name_by_id[chart_type]
            scored.append((score, catalog_id, chart_type, name, group, required, optional))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored:
        defaults = ["Bar_Chart", "Line_Chart", "Pie_Chart"]
        scored = [
            (0, catalog_id, CHART_TYPE_CONTRACTS[catalog_id][0],
             name_by_id[CHART_TYPE_CONTRACTS[catalog_id][0]][0],
             name_by_id[CHART_TYPE_CONTRACTS[catalog_id][0]][1],
             CHART_TYPE_CONTRACTS[catalog_id][1], CHART_TYPE_CONTRACTS[catalog_id][2])
            for catalog_id in defaults
        ]
    return [
        {
            "chart_id": catalog_id, "type": chart_type, "name": name, "category": group,
            "required_roles": required, "optional_roles": optional, "score": score,
        }
        for score, catalog_id, chart_type, name, group, required, optional in scored[:max(1, min(top_n, 10))]
    ]


def _safe_value(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _safe(values: list[Any]) -> list[Any]:
    return [_safe_value(value) for value in values]


def recommend(frame: pd.DataFrame, requested: str | None = None) -> str:
    valid = {item[0] for item in CHART_CATALOG}
    requested = normalize_chart_type(requested)
    if requested in valid:
        return requested
    numeric = list(frame.select_dtypes(include=np.number).columns)
    temporal = [
        column for column in frame.columns
        if pd.api.types.is_datetime64_any_dtype(frame[column])
        or any(token in str(column).lower() for token in ("date", "time", "日期", "时间", "月", "年"))
    ]
    categorical = [column for column in frame.columns if column not in numeric and column not in temporal]
    if temporal and numeric:
        return "line"
    if len(numeric) >= 2:
        return "scatter"
    if categorical and numeric:
        return "pie" if frame[categorical[0]].nunique(dropna=True) <= 6 else "bar"
    if numeric:
        return "histogram"
    return "bar"


def _base(title: str, *, category_axis: bool = True) -> dict:
    option = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 15}},
        "tooltip": {"trigger": "axis"}, "legend": {"bottom": 0},
        "grid": {"left": 48, "right": 28, "top": 52, "bottom": 58, "containLabel": True},
    }
    if category_axis:
        option.update({"xAxis": {"type": "category"}, "yAxis": {"type": "value"}})
    return option


def _quantiles(values: list[float]) -> list[float]:
    if not values:
        return [0, 0, 0, 0, 0]
    return [float(np.quantile(values, value)) for value in (0, 0.25, 0.5, 0.75, 1)]


def _kde(values: list[float], points: int = 80) -> list[list[float]]:
    if not values:
        return []
    if len(set(values)) == 1:
        return [[values[0], 1.0]]
    low, high = min(values), max(values)
    std = float(np.std(values)) or (high - low) / 6
    bandwidth = max(1e-9, 1.06 * std * len(values) ** -0.2)
    grid = np.linspace(low, high, points)
    scale = len(values) * bandwidth * math.sqrt(2 * math.pi)
    return [[float(point), float(sum(math.exp(-0.5 * ((point - value) / bandwidth) ** 2) for value in values) / scale)] for point in grid]


def _xy_data(data: pd.DataFrame, x: str, y: str) -> list[list[Any]]:
    return [
        [_safe_value(row[x]), _safe_value(row[y])]
        for _, row in data[[x, y]].dropna().iterrows()
    ]


def _number(value: Any, default: float = 0.0) -> float:
    converted = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return default if pd.isna(converted) else float(converted)


def _standard_option(
    data: pd.DataFrame,
    kind: str,
    title: str,
    x: str,
    ys: list[str],
    options: dict[str, Any],
) -> dict:
    categories = _safe(data[x].tolist())
    option = _base(title)
    option["xAxis"]["data"] = categories
    if kind == "slope" and len(ys) >= 2:
        option["xAxis"]["data"] = [ys[0], ys[1]]
        option["series"] = [
            {
                "name": str(row[x]), "type": "line", "symbolSize": 9,
                "data": [_number(row[ys[0]]), _number(row[ys[1]])],
            }
            for _, row in data.iterrows()
        ]
        return option
    if kind == "dot":
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": categories}})
        option["series"] = [
            {"name": column, "type": "scatter", "symbolSize": 10, "data": [[value, category] for category, value in zip(categories, _safe(pd.to_numeric(data[column], errors="coerce").tolist()))]}
            for column in ys
        ]
        return option
    if kind == "waterfall":
        values = [float(value or 0) for value in pd.to_numeric(data[ys[0]], errors="coerce").fillna(0)]
        measure_column = options.get("type")
        raw_measures = (
            data[str(measure_column)].astype(str).str.strip().str.lower().tolist()
            if isinstance(measure_column, str) and measure_column in data.columns
            else ["relative"] * len(values)
        )
        aliases = {
            "absolute": "absolute", "绝对": "absolute",
            "relative": "relative", "相对": "relative",
            "total": "total", "合计": "total", "总计": "total",
        }
        measures = [aliases.get(value, "relative") for value in raw_measures]
        bases: list[float] = []
        positive: list[float | None] = []
        negative: list[float | None] = []
        totals: list[float | None] = []
        running = 0.0
        for value, measure in zip(values, measures):
            if measure == "absolute":
                start, end = 0.0, value
                running = value
            elif measure == "total":
                start, end = 0.0, running
            else:
                start, end = running, running + value
                running = end
            bases.append(min(start, end))
            height = abs(end - start)
            positive.append(height if measure != "total" and end >= start else None)
            negative.append(height if measure != "total" and end < start else None)
            totals.append(height if measure == "total" else None)
        option["series"] = [
            {"name": "基准", "type": "bar", "stack": "total", "silent": True, "itemStyle": {"color": "transparent"}, "data": bases},
            {"name": "增加", "type": "bar", "stack": "total", "data": positive, "itemStyle": {"color": "#167c80"}},
            {"name": "减少", "type": "bar", "stack": "total", "data": negative, "itemStyle": {"color": "#df6b62"}},
            {"name": "合计", "type": "bar", "stack": "total", "data": totals, "itemStyle": {"color": "#355c9a"}},
        ]
        return option
    if kind == "bullet":
        actual = _safe(pd.to_numeric(data[ys[0]], errors="coerce").tolist())
        target = _safe(pd.to_numeric(data[ys[1]], errors="coerce").tolist()) if len(ys) > 1 else []
        bands = []
        for name, color, width in (
            ("high", "#d8e1e8", 34),
            ("medium", "#b9c8d2", 28),
            ("low", "#91a9b7", 22),
        ):
            column = options.get(name)
            if isinstance(column, str) and column in data.columns:
                bands.append({
                    "name": name,
                    "type": "bar",
                    "data": _safe(pd.to_numeric(data[column], errors="coerce").tolist()),
                    "barWidth": width,
                    "barGap": "-100%",
                    "silent": True,
                    "z": 1,
                    "itemStyle": {"color": color},
                })
        option["series"] = [
            *bands,
            {"name": ys[0], "type": "bar", "data": actual, "barWidth": 12, "barGap": "-100%", "z": 3},
        ]
        if target:
            option["series"].append({"name": ys[1], "type": "scatter", "data": target, "symbol": "rect", "symbolSize": [3, 30], "z": 4})
        return option
    series = []
    highlight = str(options.get("highlight") or "")
    for column in ys:
        series_type = "line" if kind in {
            "line", "area", "stacked_area", "sparkline", "slope", "bump", "horizon", "cycle",
        } else "bar"
        item = {
            "name": column, "type": series_type,
            "data": _safe(pd.to_numeric(data[column], errors="coerce").tolist()),
        }
        if kind in {"area", "stacked_area", "horizon"}:
            item["areaStyle"] = {"opacity": 0.25}
        if kind in {"stacked_area", "stacked_bar"}:
            item["stack"] = "total"
        if kind == "diverging_bar":
            item["itemStyle"] = {"color": "#167c80" if len(series) % 2 == 0 else "#df6b62"}
        if kind == "bump":
            item["symbolSize"] = 9
            item["lineStyle"] = {"width": 3}
            option["yAxis"]["inverse"] = True
            if highlight:
                selected = column == highlight
                item["lineStyle"].update(width=4 if selected else 1, opacity=1 if selected else 0.25)
                item["itemStyle"] = {"opacity": 1 if selected else 0.35}
        series.append(item)
    option["series"] = series
    if kind == "sparkline":
        option.update({"title": {"show": False}, "legend": {"show": False}, "grid": {"left": 4, "right": 4, "top": 4, "bottom": 4}})
        option["xAxis"]["show"] = option["yAxis"]["show"] = False
    return option


def _distribution_option(data: pd.DataFrame, kind: str, title: str, x: str, ys: list[str]) -> dict:
    columns = ys or [x]
    values_by_column = {
        column: [float(value) for value in pd.to_numeric(data[column], errors="coerce").dropna()]
        for column in columns
    }
    if kind == "histogram":
        values = values_by_column[columns[0]]
        counts, edges = np.histogram(values, bins=min(30, max(5, round(math.sqrt(len(values))))))
        option = _base(title)
        option["xAxis"]["data"] = [f"{edges[index]:.3g}–{edges[index + 1]:.3g}" for index in range(len(counts))]
        option["series"] = [{"name": "频数", "type": "bar", "data": counts.astype(int).tolist(), "barGap": 0}]
        return option
    if kind == "pareto":
        category = _safe(data[x].tolist())
        values = pd.to_numeric(data[ys[0]], errors="coerce").fillna(0).astype(float)
        order = np.argsort(-values.to_numpy())
        sorted_values = values.iloc[order].tolist()
        total = sum(sorted_values) or 1
        cumulative = (np.cumsum(sorted_values) / total * 100).tolist()
        option = _base(title)
        option.update({
            "xAxis": {"type": "category", "data": [category[index] for index in order]},
            "yAxis": [{"type": "value"}, {"type": "value", "max": 100, "axisLabel": {"formatter": "{value}%"}}],
            "series": [
                {"name": ys[0], "type": "bar", "data": sorted_values},
                {"name": "累计占比", "type": "line", "yAxisIndex": 1, "data": cumulative},
            ],
        })
        return option
    if kind in {"boxplot", "violin"} and ys and x not in ys:
        groups = []
        grouped_values = []
        density_series = []
        for index, (group_name, group_frame) in enumerate(data.groupby(x, dropna=False, sort=False)):
            values = [float(value) for value in pd.to_numeric(group_frame[ys[0]], errors="coerce").dropna()]
            if not values:
                continue
            groups.append(str(group_name))
            grouped_values.append(_quantiles(values))
            if kind == "violin":
                density_series.append({
                    "name": str(group_name), "type": "line", "showSymbol": False,
                    "data": [[point, density + index] for point, density in _kde(values, 35)],
                })
        option = _base(title)
        option["xAxis"]["data"] = groups
        option["series"] = [{"name": ys[0], "type": "boxplot", "data": grouped_values}]
        if density_series:
            option["density_preview"] = density_series
        return option
    if kind in {"boxplot", "violin"}:
        option = _base(title)
        option["xAxis"]["data"] = columns
        option["series"] = [{"name": "分布", "type": "boxplot", "data": [_quantiles(values_by_column[column]) for column in columns]}]
        if kind == "violin":
            option["series"].append({
                "name": "密度轮廓", "type": "scatter", "symbolSize": 3,
                "data": [[index, point[0], point[1]] for index, column in enumerate(columns) for point in _kde(values_by_column[column], 35)],
            })
        return option
    if kind == "ridgeline" and ys and x not in ys:
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "value"}, "series": []})
        for index, (group_name, group_frame) in enumerate(data.groupby(x, dropna=False, sort=False)):
            values = [float(value) for value in pd.to_numeric(group_frame[ys[0]], errors="coerce").dropna()]
            option["series"].append({
                "name": str(group_name), "type": "line", "showSymbol": False,
                "areaStyle": {"opacity": 0.2},
                "data": [[point, density + index] for point, density in _kde(values)],
            })
        return option
    if kind in {"density", "ridgeline"}:
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "value"}, "series": []})
        for index, column in enumerate(columns):
            density = _kde(values_by_column[column])
            if kind == "ridgeline":
                density = [[point, density_value + index] for point, density_value in density]
            option["series"].append({"name": column, "type": "line", "showSymbol": False, "areaStyle": {"opacity": 0.2}, "data": density})
        return option
    if kind == "beeswarm" and ys and x not in ys:
        groups = [str(value) for value in data[x].drop_duplicates().tolist()]
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": groups}, "series": []})
        for group_index, group_name in enumerate(groups):
            values = pd.to_numeric(data.loc[data[x].astype(str) == group_name, ys[0]], errors="coerce").dropna()
            option["series"].append({
                "name": group_name, "type": "scatter", "symbolSize": 6,
                "data": [[float(value), group_index + (((index * 37) % 17) - 8) / 50] for index, value in enumerate(values)],
            })
        return option
    if kind == "beeswarm":
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": columns}, "series": []})
        for index, column in enumerate(columns):
            points = [[value, index + (((position * 37) % 17) - 8) / 50] for position, value in enumerate(values_by_column[column])]
            option["series"].append({"name": column, "type": "scatter", "symbolSize": 6, "data": points})
        return option
    # error_bar: calculate quartiles from grouped raw values, or use explicit bounds.
    option = _base(title)
    if len(ys) == 1 and x != ys[0]:
        grouped = data.groupby(x, dropna=False, sort=False)[ys[0]]
        labels, center, lower, upper = [], [], [], []
        for label, values in grouped:
            numeric_values = pd.to_numeric(values, errors="coerce").dropna()
            if numeric_values.empty:
                continue
            labels.append(str(label))
            center.append(float(numeric_values.median()))
            lower.append(float(numeric_values.quantile(0.25)))
            upper.append(float(numeric_values.quantile(0.75)))
        option["xAxis"]["data"] = labels
        option["series"] = [
            {"name": "Q25", "type": "line", "data": lower, "lineStyle": {"type": "dashed"}, "symbol": "none"},
            {"name": "Q75", "type": "line", "data": upper, "lineStyle": {"type": "dashed"}, "areaStyle": {"opacity": 0.12}, "symbol": "none"},
            {"name": "中位数", "type": "scatter", "data": center, "symbolSize": 9},
        ]
        return option
    option["xAxis"]["data"] = _safe(data[x].tolist())
    center = pd.to_numeric(data[ys[0]], errors="coerce").fillna(0)
    lower = pd.to_numeric(data[ys[1]], errors="coerce").fillna(center) if len(ys) > 1 else center
    upper = pd.to_numeric(data[ys[2]], errors="coerce").fillna(center) if len(ys) > 2 else center
    # JSON 图表规格不携带函数，用上下界线和中心点表达可移植的误差范围。
    option["series"] = [
        {
            "name": "下界", "type": "line", "data": lower.astype(float).tolist(),
            "lineStyle": {"type": "dashed", "opacity": 0.65}, "symbol": "none",
        },
        {
            "name": "上界", "type": "line", "data": upper.astype(float).tolist(),
            "lineStyle": {"type": "dashed", "opacity": 0.65}, "symbol": "none",
            "areaStyle": {"opacity": 0.12},
        },
        {"name": ys[0], "type": "scatter", "data": center.astype(float).tolist(), "symbolSize": 9},
    ]
    return option


def _relationship_option(
    data: pd.DataFrame,
    kind: str,
    title: str,
    x: str,
    ys: list[str],
    columns: list[str],
    options: dict[str, Any],
) -> dict:
    if kind in {"scatter", "bubble", "connected_scatter"}:
        y = ys[0]
        option = _base(title, category_axis=False)
        point_columns = [x, y]
        size_field = ys[1] if len(ys) > 1 else None
        if size_field:
            point_columns.append(size_field)
        color_field = options.get("color")
        if not isinstance(color_field, str) or color_field not in data.columns:
            color_field = None
        if color_field:
            point_columns.append(color_field)
        order_column = options.get("order")
        if kind == "connected_scatter" and isinstance(order_column, str) and order_column in data.columns:
            point_columns.append(order_column)
        point_rows = data[list(dict.fromkeys(point_columns))].dropna(subset=[x, y])
        if kind == "connected_scatter" and isinstance(order_column, str) and order_column in point_rows.columns:
            point_rows = point_rows.sort_values(order_column, kind="stable")
        points = [[_number(row[x]), _number(row[y])] for _, row in point_rows.iterrows()]
        series_type = "line" if kind == "connected_scatter" else "scatter"
        series = {"name": f"{x} × {y}", "type": series_type, "data": points, "symbolSize": 9}
        sizes = None
        if size_field:
            sizes = pd.to_numeric(point_rows[size_field], errors="coerce").fillna(0).abs()
            maximum = float(sizes.max()) or 1
            series["data"] = [
                {
                    "value": [*point, float(sizes.iloc[index])],
                    "symbolSize": 6 + math.sqrt(float(sizes.iloc[index]) / maximum) * 34,
                }
                for index, point in enumerate(points)
            ]
            series["size_field"] = size_field
        if color_field:
            color_values = point_rows[color_field]
            numeric_colors = pd.to_numeric(color_values, errors="coerce")
            if numeric_colors.notna().all():
                visual_dimension = 3 if sizes is not None else 2
                for index, item in enumerate(series["data"]):
                    if not isinstance(item, dict):
                        item = {"value": item}
                        series["data"][index] = item
                    item["value"].append(float(numeric_colors.iloc[index]))
                option["visualMap"] = {
                    "dimension": visual_dimension,
                    "min": float(numeric_colors.min()),
                    "max": float(numeric_colors.max()),
                    "calculable": True,
                    "right": 8,
                }
            else:
                palette = ["#167c80", "#355c9a", "#df6b62", "#d69e2e", "#805ad5", "#2f855a", "#c05621"]
                color_names = ["未分类" if pd.isna(value) else str(value) for value in color_values]
                categories = list(dict.fromkeys(color_names))
                colors = {value: palette[index % len(palette)] for index, value in enumerate(categories)}
                for index, item in enumerate(series["data"]):
                    if not isinstance(item, dict):
                        item = {"value": item}
                        series["data"][index] = item
                    item["name"] = color_names[index]
                    item["itemStyle"] = {"color": colors[color_names[index]]}
                option["legend"] = {"bottom": 0, "data": categories}
            series["color_field"] = color_field
        if kind == "bubble":
            mark_lines = []
            for option_name, axis_name in (("x_mid", "xAxis"), ("y_mid", "yAxis")):
                raw_value = options.get(option_name)
                if isinstance(raw_value, str) and raw_value in data.columns:
                    values = pd.to_numeric(data[raw_value], errors="coerce").dropna()
                    raw_value = values.iloc[0] if not values.empty else None
                if raw_value is None:
                    for column in data.columns:
                        normalized = str(column).lower()
                        if normalized == option_name or (
                            option_name[0] in normalized and ("mid" in normalized or "中线" in normalized)
                        ):
                            values = pd.to_numeric(data[column], errors="coerce").dropna()
                            if not values.empty:
                                raw_value = values.iloc[0]
                                break
                value = pd.to_numeric(pd.Series([raw_value]), errors="coerce").iloc[0]
                if not pd.isna(value):
                    mark_lines.append({axis_name: float(value), "name": option_name})
            if mark_lines:
                series["markLine"] = {
                    "silent": True,
                    "symbol": "none",
                    "lineStyle": {"type": "dashed", "color": "#718096"},
                    "data": mark_lines,
                }
        option.update({"xAxis": {"type": "value", "name": x}, "yAxis": {"type": "value", "name": y}, "series": [series]})
        return option
    if kind == "heatmap":
        if len(columns) < 3:
            raise ValueError("热力图需要两个分类字段和一个数值字段")
        x_values = list(dict.fromkeys(str(value) for value in data[columns[0]].tolist()))
        y_values = list(dict.fromkeys(str(value) for value in data[columns[1]].tolist()))
        values = [
            [
                x_values.index(str(row[columns[0]])),
                y_values.index(str(row[columns[1]])),
                _number(row[columns[2]]),
            ]
            for _, row in data.iterrows()
        ]
        maximum = max([value[2] for value in values] or [1])
        option = _base(title)
        option.update({
            "xAxis": {"type": "category", "data": x_values}, "yAxis": {"type": "category", "data": y_values},
            "visualMap": {"min": 0, "max": maximum, "calculable": True, "orient": "horizontal", "left": "center", "bottom": 0},
            "series": [{"type": "heatmap", "data": values}],
        })
        return option
    if kind == "parallel":
        color_field = options.get("color")
        if not isinstance(color_field, str) or color_field not in data.columns:
            color_field = None
        dimensions = [column for column in columns if column != color_field][:8]
        axes = []
        for index, column in enumerate(dimensions):
            numeric = pd.api.types.is_numeric_dtype(data[column])
            axes.append({
                "dim": index, "name": column, "type": "value" if numeric else "category",
                **({} if numeric else {"data": list(dict.fromkeys(str(value) for value in data[column]))[:80]}),
            })
        series = []
        groups = data.groupby(color_field, dropna=False, sort=False) if color_field else [("全部", data)]
        for group_name, group_frame in groups:
            series.append({
                "name": str(group_name), "type": "parallel",
                "data": [[_safe_value(row[column]) for column in dimensions] for _, row in group_frame.iterrows()],
            })
        return {
            "title": {"text": title}, "parallelAxis": axes,
            "legend": {"bottom": 0, "show": bool(color_field)}, "series": series,
        }
    if kind in {"network", "chord", "sankey"}:
        if len(columns) < 2:
            raise ValueError(f"{kind} 需要来源和目标字段")
        names = list(dict.fromkeys(str(value) for value in pd.concat([data[columns[0]], data[columns[1]]])))
        links = [
            {
                "source": str(row[columns[0]]), "target": str(row[columns[1]]),
                "value": float(row[columns[2]]) if len(columns) > 2 and pd.notna(row[columns[2]]) else 1,
            }
            for _, row in data.iterrows()
        ]
        series_type = "sankey" if kind == "sankey" else "graph"
        series = {"type": series_type, "data": [{"name": name} for name in names], "links": links, "roam": True, "label": {"show": True}}
        if kind == "network":
            series.update({"layout": "force", "force": {"repulsion": 160, "edgeLength": 90}})
        if kind == "chord":
            series.update({"layout": "circular", "circular": {"rotateLabel": True}, "lineStyle": {"curveness": 0.5, "opacity": 0.45}})
        return {"title": {"text": title}, "tooltip": {}, "series": [series]}
    # radar
    indicators = []
    for column in ys:
        values = pd.to_numeric(data[column], errors="coerce").fillna(0).abs()
        indicators.append({"name": column, "max": float(values.max()) or 1})
    radar_data = [
        {"name": str(row[x]), "value": [_number(row[column]) for column in ys]}
        for _, row in data.head(12).iterrows()
    ]
    return {"title": {"text": title}, "tooltip": {}, "legend": {"bottom": 0}, "radar": {"indicator": indicators}, "series": [{"type": "radar", "data": radar_data}]}


def _composition_option(
    data: pd.DataFrame, kind: str, title: str, x: str, ys: list[str], options: dict, group: str | None,
) -> dict:
    y = ys[0]
    values = pd.to_numeric(data[y], errors="coerce").fillna(0).astype(float).tolist()
    names = [str(value) for value in data[x].tolist()]
    items = [{"name": name, "value": value} for name, value in zip(names, values)]
    if kind == "gauge":
        maximum = float(options.get("max") or max(values or [100]) or 100)
        return {"title": {"text": title}, "series": [{"type": "gauge", "max": maximum, "progress": {"show": True}, "detail": {"valueAnimation": True}, "data": items[:1]}]}
    if kind == "pyramid" and len(ys) >= 2:
        left = [-abs(value) for value in pd.to_numeric(data[ys[0]], errors="coerce").fillna(0).astype(float)]
        right = [abs(value) for value in pd.to_numeric(data[ys[1]], errors="coerce").fillna(0).astype(float)]
        return {
            "title": {"text": title}, "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "value", "axisLabel": {"formatter": "{value}"}},
            "yAxis": {"type": "category", "data": names},
            "series": [
                {"name": ys[0], "type": "bar", "stack": "total", "data": left},
                {"name": ys[1], "type": "bar", "stack": "total", "data": right},
            ],
        }
    if kind in {"funnel", "pyramid"}:
        return {"title": {"text": title}, "tooltip": {}, "series": [{"type": "funnel", "sort": "ascending" if kind == "pyramid" else "descending", "data": items}]}
    if kind in {"treemap", "sunburst"}:
        parent_column = str(options.get("parents") or "")
        if parent_column in data.columns:
            node_by_name = {item["name"]: {**item, "children": []} for item in items}
            roots = []
            for row, item in zip(data.to_dict("records"), items):
                node = node_by_name[item["name"]]
                parent = str(row.get(parent_column) or "")
                if parent and parent in node_by_name and parent != item["name"]:
                    node_by_name[parent]["children"].append(node)
                else:
                    roots.append(node)
            items = roots
        return {"title": {"text": title}, "tooltip": {}, "series": [{"type": kind, "data": items, "radius": ["10%", "78%"]}]}
    if kind == "waffle":
        total = sum(max(0, value) for value in values) or 1
        points = []
        cursor = 0
        for name, value in zip(names, values):
            count = round(max(0, value) / total * 100)
            points.extend({"name": name, "value": [index % 10, index // 10, value]} for index in range(cursor, min(100, cursor + count)))
            cursor += count
        return {"title": {"text": title}, "tooltip": {}, "xAxis": {"show": False, "min": -1, "max": 10}, "yAxis": {"show": False, "min": -1, "max": 10}, "series": [{"type": "scatter", "symbol": "rect", "symbolSize": 18, "data": points}]}
    if kind == "marimekko" and group and group in data.columns:
        pivot = data.pivot_table(index=x, columns=group, values=y, aggfunc="sum", fill_value=0, sort=False)
        if options.get("percent"):
            pivot = pivot.div(pivot.sum(axis=1).replace(0, 1), axis=0) * 100
        return {
            "title": {"text": title}, "tooltip": {"trigger": "axis"}, "legend": {"bottom": 0},
            "xAxis": {"type": "category", "data": [str(value) for value in pivot.index]},
            "yAxis": {"type": "value", "max": 100 if options.get("percent") else None},
            "series": [
                {"name": str(column), "type": "bar", "stack": "mosaic", "data": _safe(pivot[column].tolist())}
                for column in pivot.columns
            ],
        }
    if kind == "marimekko":
        total = sum(max(value, 0) for value in values) or 1
        widths = [max(value, 0) / total * 100 for value in values]
        # 单指标时，100% 宽度马赛克等价于按占比排列的累积条。
        return {
            "title": {"text": title}, "tooltip": {"trigger": "item"},
            "xAxis": {"type": "value", "max": 100, "axisLabel": {"formatter": "{value}%"}},
            "yAxis": {"type": "category", "data": ["总体"]},
            "series": [
                {
                    "name": name, "type": "bar", "stack": "mosaic", "data": [width],
                    "label": {"show": width >= 6, "formatter": f"{name}\n{width:.1f}%"},
                }
                for name, width in zip(names, widths)
            ],
        }
    return {
        "title": {"text": title}, "tooltip": {"trigger": "item"}, "legend": {"bottom": 0},
        "series": [{
            "type": "pie", "data": items, "radius": ["42%", "70%"] if kind == "donut" else [0, "70%"],
            **({"roseType": "area"} if kind == "rose" else {}),
        }],
    }


def _special_option(data: pd.DataFrame, kind: str, title: str, x: str, ys: list[str], options: dict) -> dict:
    if kind == "circular_line":
        return {
            "title": {"text": title}, "tooltip": {}, "angleAxis": {"type": "category", "data": _safe(data[x].tolist())},
            "radiusAxis": {}, "polar": {},
            "series": [{"name": column, "type": "line", "coordinateSystem": "polar", "data": _safe(pd.to_numeric(data[column], errors="coerce").tolist())} for column in ys],
        }
    if kind == "candlestick":
        numeric = [column for column in data.columns if pd.api.types.is_numeric_dtype(data[column])]
        if len(numeric) < 4:
            raise ValueError("K 线图需要开盘、收盘、最低、最高四个数值字段")
        selected = ys[:4] if len(ys) >= 4 else numeric[:4]
        return {
            "title": {"text": title}, "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": _safe(data[x].tolist())}, "yAxis": {"scale": True},
            "series": [{"type": "candlestick", "name": "OHLC", "data": [[float(row[column]) for column in selected] for _, row in data.iterrows()]}],
        }
    if kind == "calendar":
        dates = pd.to_datetime(data[x], errors="coerce")
        year = int(dates.dropna().iloc[0].year) if dates.notna().any() else pd.Timestamp.now().year
        values = pd.to_numeric(data[ys[0]], errors="coerce").fillna(0)
        points = [[date.strftime("%Y-%m-%d"), float(value)] for date, value in zip(dates, values) if pd.notna(date)]
        return {
            "title": {"text": title}, "tooltip": {},
            "visualMap": {"min": float(values.min()), "max": float(values.max()) or 1, "orient": "horizontal", "left": "center", "bottom": 0},
            "calendar": {"range": year, "cellSize": ["auto", 18]},
            "series": [{"type": "heatmap", "coordinateSystem": "calendar", "data": points}],
        }
    if kind in {"choropleth", "dot_map"}:
        values = pd.to_numeric(data[ys[0]], errors="coerce").fillna(0)
        if kind == "choropleth":
            return {
                "title": {"text": title}, "tooltip": {},
                "visualMap": {"min": float(values.min()), "max": float(values.max()) or 1},
                "series": [{
                    "type": "map", "map": options.get("map", "china"), "roam": True,
                    "data": [{"name": str(name), "value": float(value)} for name, value in zip(data[x], values)],
                }],
            }
        if len(ys) < 2:
            try:
                from pyecharts.datasets import COORDINATES
            except ImportError:
                return {
                    "title": {"text": title}, "tooltip": {},
                    "visualMap": {"min": float(values.min()), "max": float(values.max()) or 1},
                    "series": [{
                        "type": "map", "map": options.get("map", "china"), "roam": True,
                        "data": [
                            {"name": str(name), "value": float(value)}
                            for name, value in zip(data[x], values)
                        ],
                    }],
                    "_meridian_warnings": ["当前环境未安装 pyecharts，点密度地图已降级为区域地图。"],
                }

            category_field = options.get("category")
            if not isinstance(category_field, str) or category_field not in data.columns:
                category_field = None
            rows: list[tuple[str, list[float], float, str]] = []
            missing = []
            for index, (_, row) in enumerate(data.iterrows()):
                name = str(row[x]).strip()
                coordinates = COORDINATES.get(name)
                if coordinates is None:
                    for suffix in ("特别行政区", "自治区", "自治州", "省", "市", "区", "县", "镇"):
                        if name.endswith(suffix):
                            coordinates = COORDINATES.get(name.removesuffix(suffix))
                            if coordinates is not None:
                                break
                if coordinates is None:
                    missing.append(name)
                    continue
                category = str(row[category_field]) if category_field and pd.notna(row[category_field]) else "全部"
                rows.append((name, [float(coordinates[0]), float(coordinates[1])], float(values.iloc[index]), category))
            maximum = max((abs(item[2]) for item in rows), default=1) or 1
            groups = list(dict.fromkeys(item[3] for item in rows))
            series = []
            for group_name in groups:
                points = [
                    {"name": name, "value": [*coordinates, value], "symbolSize": 5 + math.sqrt(abs(value) / maximum) * 23}
                    for name, coordinates, value, category in rows if category == group_name
                ]
                series.append({
                    "name": group_name, "type": "effectScatter", "coordinateSystem": "geo",
                    "showEffectOn": "emphasis", "data": points,
                })
            return {
                "title": {"text": title}, "tooltip": {},
                "legend": {"bottom": 0, "show": bool(category_field)},
                "geo": {"map": options.get("map", "china"), "roam": True},
                "visualMap": {"dimension": 2, "min": 0, "max": maximum, "calculable": True},
                "series": series or [{"name": "全部", "type": "effectScatter", "coordinateSystem": "geo", "data": []}],
                "_meridian_warnings": [f"未找到地理坐标：{', '.join(dict.fromkeys(missing))}"] if missing else [],
            }
        sizes = pd.to_numeric(data[ys[2]], errors="coerce").fillna(1) if len(ys) > 2 else pd.Series([1] * len(data))
        return {
            "title": {"text": title}, "tooltip": {}, "geo": {"map": options.get("map", "china"), "roam": True},
            "series": [{"type": "scatter", "coordinateSystem": "geo", "data": [[float(lon), float(lat), float(size)] for lon, lat, size in zip(data[ys[0]], data[ys[1]], sizes)]}],
        }
    raise ValueError(f"无法生成图表：{kind}")


def make_spec(
    frame: pd.DataFrame,
    *,
    chart_type: str | None = None,
    title: str = "分析结果",
    x: str | None = None,
    y: list[str] | str | None = None,
    group: str | None = None,
    options: dict | None = None,
) -> dict:
    if frame.empty:
        raise ValueError("空结果无法生成图表")
    kind = recommend(frame, chart_type)
    columns = [str(column) for column in frame.columns]
    data = frame.head(1000).copy()
    data.columns = columns
    numeric = [str(column) for column in data.select_dtypes(include=np.number).columns]
    non_numeric = [column for column in columns if column not in numeric]
    relationship = kind in {"scatter", "bubble", "connected_scatter"}
    if relationship:
        x = x if x in numeric else (numeric[0] if numeric else None)
    else:
        x = x if x in columns else (non_numeric[0] if non_numeric else columns[0])
    if not x:
        raise ValueError(f"{kind} 没有可用的 X 字段")
    y_values = [y] if isinstance(y, str) else list(y or [])
    y_values = [column for column in y_values if column in columns and column != x]
    if not y_values:
        y_values = [column for column in numeric if column != x][:8]
    if not y_values and kind not in {"histogram", "boxplot", "violin", "density", "ridgeline", "beeswarm"}:
        if len(columns) < 2:
            data["记录数"] = 1
            columns.append("记录数")
            numeric.append("记录数")
        y_values = [next(column for column in columns if column != x)]
    source_group = group if group in columns and group != x else None
    if source_group and len(y_values) == 1 and kind in {
        "bar", "grouped_bar", "stacked_bar", "line", "area", "stacked_area", "bump", "horizon", "cycle",
    }:
        pivot = data.pivot_table(
            index=x, columns=source_group, values=y_values[0], aggfunc="sum", fill_value=0, sort=False,
        ).reset_index()
        pivot.columns = [str(value) for value in pivot.columns]
        data = pivot
        columns = [str(column) for column in data.columns]
        numeric = [str(column) for column in data.select_dtypes(include=np.number).columns]
        y_values = [column for column in columns if column != x]
    options = options or {}
    standard = {
        "bar", "grouped_bar", "stacked_bar", "diverging_bar", "bullet", "dot", "slope", "waterfall",
        "line", "area", "stacked_area", "sparkline", "bump", "horizon", "cycle",
    }
    distribution = {"histogram", "pareto", "boxplot", "violin", "ridgeline", "beeswarm", "error_bar", "density"}
    relationship_types = {"scatter", "bubble", "connected_scatter", "heatmap", "parallel", "network", "chord", "sankey", "radar"}
    composition = {"pie", "donut", "rose", "treemap", "sunburst", "waffle", "marimekko", "funnel", "pyramid", "gauge"}
    if kind in standard:
        option = _standard_option(data, kind, title, x, y_values, options)
    elif kind in distribution:
        option = _distribution_option(data, kind, title, x, y_values)
    elif kind in relationship_types:
        option = _relationship_option(data, kind, title, x, y_values, columns, options)
    elif kind in composition:
        option = _composition_option(data, kind, title, x, y_values, options, source_group)
    else:
        option = _special_option(data, kind, title, x, y_values, options)
    warnings = option.pop("_meridian_warnings", [])
    if len(frame) > 1000:
        warnings.append("图表仅展示前 1000 行；请先聚合或筛选以避免视觉误导。")
    spec = {
        "type": kind, "title": title, "x": x, "y": y_values,
        "group": source_group, "columns": columns,
        "records": frame_records(data, 1000), "options": options, "option": option,
        "warnings": warnings,
        "encoding": {
            "x": _safe(data[x].tolist()) if x in data else [],
            "series": [
                {"name": column, "values": _safe(pd.to_numeric(data[column], errors="coerce").tolist())}
                for column in y_values
            ],
        },
    }
    return spec
