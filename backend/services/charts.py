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


def catalog() -> list[dict]:
    return [{"id": chart_id, "name": name, "group": group} for chart_id, name, group in CHART_CATALOG]


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


def _standard_option(data: pd.DataFrame, kind: str, title: str, x: str, ys: list[str]) -> dict:
    categories = _safe(data[x].tolist())
    option = _base(title)
    option["xAxis"]["data"] = categories
    if kind == "dot":
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": categories}})
        option["series"] = [
            {"name": column, "type": "scatter", "symbolSize": 10, "data": [[value, category] for category, value in zip(categories, _safe(pd.to_numeric(data[column], errors="coerce").tolist()))]}
            for column in ys
        ]
        return option
    if kind == "waterfall":
        values = [float(value or 0) for value in pd.to_numeric(data[ys[0]], errors="coerce").fillna(0)]
        bases, running = [], 0.0
        for value in values:
            bases.append(running if value >= 0 else running + value)
            running += value
        option["series"] = [
            {"name": "基准", "type": "bar", "stack": "total", "silent": True, "itemStyle": {"color": "transparent"}, "data": bases},
            {"name": ys[0], "type": "bar", "stack": "total", "data": [abs(value) for value in values], "itemStyle": {"color": "#167c80"}},
        ]
        return option
    if kind == "bullet":
        actual = _safe(pd.to_numeric(data[ys[0]], errors="coerce").tolist())
        target = _safe(pd.to_numeric(data[ys[1]], errors="coerce").tolist()) if len(ys) > 1 else []
        option["series"] = [{"name": ys[0], "type": "bar", "data": actual, "barWidth": 18}]
        if target:
            option["series"].append({"name": ys[1], "type": "scatter", "data": target, "symbol": "rect", "symbolSize": [3, 28]})
        return option
    series = []
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
    if kind in {"density", "ridgeline"}:
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "value"}, "series": []})
        for index, column in enumerate(columns):
            density = _kde(values_by_column[column])
            if kind == "ridgeline":
                density = [[point, density_value + index] for point, density_value in density]
            option["series"].append({"name": column, "type": "line", "showSymbol": False, "areaStyle": {"opacity": 0.2}, "data": density})
        return option
    if kind == "beeswarm":
        option = _base(title, category_axis=False)
        option.update({"xAxis": {"type": "value"}, "yAxis": {"type": "category", "data": columns}, "series": []})
        for index, column in enumerate(columns):
            points = [[value, index + (((position * 37) % 17) - 8) / 50] for position, value in enumerate(values_by_column[column])]
            option["series"].append({"name": column, "type": "scatter", "symbolSize": 6, "data": points})
        return option
    # error_bar: value plus explicit lower/upper columns when present.
    option = _base(title)
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


def _relationship_option(data: pd.DataFrame, kind: str, title: str, x: str, ys: list[str], columns: list[str]) -> dict:
    if kind in {"scatter", "bubble", "connected_scatter"}:
        y = ys[0]
        option = _base(title, category_axis=False)
        point_columns = [x, y]
        if kind == "bubble" and len(ys) > 1:
            point_columns.append(ys[1])
        point_rows = data[point_columns].dropna(subset=[x, y])
        points = [[_number(row[x]), _number(row[y])] for _, row in point_rows.iterrows()]
        series_type = "line" if kind == "connected_scatter" else "scatter"
        series = {"name": f"{x} × {y}", "type": series_type, "data": points, "symbolSize": 9}
        if kind == "bubble" and len(ys) > 1:
            sizes = pd.to_numeric(point_rows[ys[1]], errors="coerce").fillna(0).abs()
            maximum = float(sizes.max()) or 1
            series["data"] = [
                {
                    "value": [*point, float(sizes.iloc[index])],
                    "symbolSize": 6 + math.sqrt(float(sizes.iloc[index]) / maximum) * 34,
                }
                for index, point in enumerate(points)
            ]
            series["size_field"] = ys[1]
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
        axes = []
        for index, column in enumerate(columns[:8]):
            numeric = pd.api.types.is_numeric_dtype(data[column])
            axes.append({
                "dim": index, "name": column, "type": "value" if numeric else "category",
                **({} if numeric else {"data": list(dict.fromkeys(str(value) for value in data[column]))[:80]}),
            })
        return {
            "title": {"text": title}, "parallelAxis": axes,
            "series": [{"type": "parallel", "data": [[_safe_value(row[column]) for column in columns[:8]] for _, row in data.iterrows()]}],
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


def _composition_option(data: pd.DataFrame, kind: str, title: str, x: str, y: str, options: dict) -> dict:
    values = pd.to_numeric(data[y], errors="coerce").fillna(0).astype(float).tolist()
    names = [str(value) for value in data[x].tolist()]
    items = [{"name": name, "value": value} for name, value in zip(names, values)]
    if kind == "gauge":
        maximum = float(options.get("max") or max(values or [100]) or 100)
        return {"title": {"text": title}, "series": [{"type": "gauge", "max": maximum, "progress": {"show": True}, "detail": {"valueAnimation": True}, "data": items[:1]}]}
    if kind in {"funnel", "pyramid"}:
        return {"title": {"text": title}, "tooltip": {}, "series": [{"type": "funnel", "sort": "ascending" if kind == "pyramid" else "descending", "data": items}]}
    if kind in {"treemap", "sunburst"}:
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
                "series": [{"type": "map", "map": options.get("map", "china"), "roam": True, "data": [{"name": str(name), "value": float(value)} for name, value in zip(data[x], values)]}],
            }
        if len(ys) < 2:
            raise ValueError("点密度地图需要经度和纬度字段")
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
    options = options or {}
    standard = {
        "bar", "grouped_bar", "stacked_bar", "diverging_bar", "bullet", "dot", "slope", "waterfall",
        "line", "area", "stacked_area", "sparkline", "bump", "horizon", "cycle",
    }
    distribution = {"histogram", "pareto", "boxplot", "violin", "ridgeline", "beeswarm", "error_bar", "density"}
    relationship_types = {"scatter", "bubble", "connected_scatter", "heatmap", "parallel", "network", "chord", "sankey", "radar"}
    composition = {"pie", "donut", "rose", "treemap", "sunburst", "waffle", "marimekko", "funnel", "pyramid", "gauge"}
    if kind in standard:
        option = _standard_option(data, kind, title, x, y_values)
    elif kind in distribution:
        option = _distribution_option(data, kind, title, x, y_values)
    elif kind in relationship_types:
        option = _relationship_option(data, kind, title, x, y_values, columns)
    elif kind in composition:
        option = _composition_option(data, kind, title, x, y_values[0], options)
    else:
        option = _special_option(data, kind, title, x, y_values, options)
    warnings = []
    if len(frame) > 1000:
        warnings.append("图表仅展示前 1000 行；请先聚合或筛选以避免视觉误导。")
    spec = {
        "type": kind, "title": title, "x": x, "y": y_values,
        "group": group if group in columns else None, "columns": columns,
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
