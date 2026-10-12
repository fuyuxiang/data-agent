from __future__ import annotations

import hashlib
import base64
import html
import math
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Inches, Pt as DocPt, RGBColor
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.util import Inches as PptInches, Pt
from flask import current_app

from ...agent.store import RunStore
from ...core.database import Database, utcnow
from ..security import SecretVault
from .manifests import ResultService
from ..query_results import read_result_frame


ARTIFACT_KINDS = (
    "summary_docx", "report_docx", "report_html",
    "dashboard_png", "data_xlsx", "report_pptx",
)
DEFAULT_ARTIFACT_KINDS = ARTIFACT_KINDS[:3]


def _safe_excel_text(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _published(database: Database, run_id: str, workspace_id: str) -> tuple[dict, dict]:
    service = ResultService(database)
    publication = service.publication(run_id, workspace_id=workspace_id)
    if not publication:
        raise PermissionError("未通过验证发布门禁，不得导出或发送正式成果")
    manifest = service.manifest(publication["manifest_id"], workspace_id=workspace_id)
    if not manifest or manifest.get("status") != "published":
        raise PermissionError("发布版本已失效")
    return publication, manifest


def _record_artifact(
    database: Database, path: Path, kind: str, workspace_id: str,
    run_id: str, publication: dict, manifest: dict,
) -> dict[str, Any]:
    digest = _sha256(path)
    run = RunStore(database).get_run(run_id, workspace_id=workspace_id, include_archived=True)
    if not run:
        raise FileNotFoundError("分析任务不存在")
    source_scope = run.get("source_scope") or []
    labels = {"summary_docx": "结论摘要", "report_docx": "分析报告", "report_html": "网页报告",
              "dashboard_png": "数据看板", "data_xlsx": "分析数据", "report_pptx": "汇报 PPT"}
    subject = str(manifest.get("payload", {}).get("title") or "数据分析")
    title = f"{subject} · {labels.get(kind, '成果文件')}"
    safe_name = re.sub(r'[\x00-\x1f/\\<>:"|?*]', '_', subject).strip(' .')[:100] or "数据分析"
    safe_name = safe_name.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    filename = f"{safe_name}_{labels.get(kind, '成果')}_v{manifest['version']}{path.suffix}"
    return database.put("artifacts", {
        "id": database.new_id("art"), "workspace_id": workspace_id,
        "run_id": run_id, "publication_id": publication["id"], "manifest_id": manifest["id"],
        "manifest_version": manifest["version"], "title": title, "kind": kind,
        "filename": filename, "path": str(path), "size": path.stat().st_size,
        "sha256": digest, "status": "ready", "immutable": True,
        "source_ids": list(source_scope),
    }, workspace_id=workspace_id)


def _doc_header(document: Document, manifest: dict, title: str) -> dict[str, Any]:
    payload = manifest["payload"]
    contract = payload["contract"]
    title_style = document.styles["Title"]
    title_style.font.color.rgb = RGBColor(0, 0, 0)
    title_style.font.size = DocPt(24)
    title_style.font.underline = False
    if title_style.element.pPr is not None:
        for border in list(title_style.element.pPr.findall(qn("w:pBdr"))):
            title_style.element.pPr.remove(border)
    document.styles["Normal"].font.size = DocPt(11)
    document.styles["Normal"].paragraph_format.space_after = DocPt(6)
    document.styles["Normal"].paragraph_format.line_spacing = 1.15
    title_style.paragraph_format.space_after = DocPt(12)
    for name, size, before, after in (("Heading 1", 17, 14, 6), ("Heading 2", 14, 10, 4)):
        document.styles[name].font.size = DocPt(size)
        document.styles[name].paragraph_format.space_before = DocPt(before)
        document.styles[name].paragraph_format.space_after = DocPt(after)
    document.add_heading(title, 0)
    document.add_paragraph(f"成果版本：{manifest['version']}  生成时间：{manifest['created_at']}")
    document.add_heading("业务分析目标", level=1)
    document.add_paragraph(str(contract.get("objective") or ""))
    document.add_heading("统计覆盖范围", level=1)
    document.add_paragraph(str(contract.get("coverage") or ""))
    dimensions = contract.get("dimensions") or []
    if dimensions:
        document.add_paragraph("查看维度：" + "、".join(str(value) for value in dimensions))
    return payload


def _write_kpis(document: Document, kpis: list[dict[str, Any]]) -> None:
    if not kpis:
        return
    document.add_heading("关键指标", level=1)
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ("指标", "数值", "单位")):
        cell.text = label
    for item in kpis:
        cells = table.add_row().cells
        cells[0].text = str(item.get("label") or "不可用")
        cells[1].text = str(_kpi_display(item))
        cells[2].text = str(item.get("unit") or "未注明")


def _kpi_display(item: dict) -> Any:
    display = item.get("display")
    if display is not None and display != "":
        return display
    return item["value"] if item.get("value") is not None else "缺失"


def _report_sections(payload: dict) -> list[tuple[str, list[str]]]:
    report = payload.get("report") or {}
    sections = []
    findings = report.get("findings") or report.get("attribution") or []
    if findings:
        lines = []
        labels = {"fact": "事实", "inference": "推测", "risk": "风险"}
        confidence = {"high": "高", "medium": "中", "low": "低"}
        for item in findings:
            if isinstance(item, str):
                lines.append(item)
                continue
            line = f"[{labels.get(item.get('type'), '发现')}] {item.get('text', '')}"
            if item.get("confidence") in confidence:
                line += f"（置信度：{confidence[item['confidence']]}；{item.get('confidence_reason') or '依据待核对'}）"
            lines.append(line)
            for key, label in (("why", "解释"), ("impact", "影响")):
                if item.get(key):
                    lines.append(f"{label}：{item[key]}")
        sections.append(("发现与解释", lines))
    actions = []
    for item in report.get("actions") or []:
        context = "；".join(f"{label}：{item[key]}" for key, label in (("owner", "执行对象"), ("scope", "范围"))
                            if item.get(key))
        actions.append(str(item.get("text") or "") + (f"（{context}）" if context else ""))
    for key, label in (("short_term", "短期"), ("medium_term", "中期"), ("long_term", "长期")):
        actions.extend(f"{label}：{text}" for text in (report.get("recommendations") or {}).get(key) or [])
    if actions:
        sections.append(("行动建议", list(dict.fromkeys(actions))))
    if payload.get("data_quality"):
        quality = payload["data_quality"]
        lines = [quality["summary"]]
        for check in quality.get("checks") or []:
            details = [f"{label}：{check[key]}" for key, label in
                       (("rows", "记录数"), ("missing_cells", "缺失单元格"), ("duplicate_rows", "重复行"))
                       if check.get(key) is not None]
            lines.append(str(check.get("scope") or "探查范围") + "；" + "；".join(details)
                         + (f"；{check['note']}" if check.get("note") else ""))
        sections.append(("数据质量说明", lines))
    for key, label in (("methods", "分析方法"), ("assumptions", "口径与假设")):
        if report.get(key):
            sections.append((label, [str(value) for value in report[key]]))
    glossary = []
    for item in payload.get("glossary") or []:
        line = str(item.get("name") or "") + "：" + str(item.get("definition") or "定义待核对")
        if item.get("unit"):
            line += f"；单位：{item['unit']}"
        if item.get("formula"):
            line += f"；计算方式：{item['formula']}"
        if item.get("notes"):
            line += f"；说明：{item['notes']}"
        if item.get("definition_status") != "governed":
            line += "；探索口径，待核对"
        glossary.append(line)
    if glossary:
        sections.append(("指标与字段说明", glossary))
    return sections


def _ppt_text(slides: Presentation, title: str, lines: list[str]) -> None:
    """Paginate instead of silently clipping long conclusions or definitions."""
    chunks = []
    for line in lines:
        for paragraph in str(line).splitlines():
            chunks.extend(paragraph[start:start + 65] for start in range(0, len(paragraph), 65))
    page_count = max(1, math.ceil(len(chunks) / 5))
    page_size = max(1, math.ceil(len(chunks) / page_count))
    for offset in range(0, len(chunks), page_size):
        slide = slides.slides.add_slide(slides.slide_layouts[1])
        slide.shapes.title.text = title + (f" · {offset // page_size + 1}" if page_count > 1 else "")
        slide.placeholders[1].text = "\n".join(chunks[offset:offset + page_size])


def _write_data_preview(document: Document, tables: list[dict]) -> None:
    for item in tables:
        columns = item.get("columns") or []
        rows = item.get("rows") or []
        if not columns or not rows:
            continue
        document.add_heading(str(item.get("title") or "数据明细"), level=2)
        # Wide detail remains available in the complete Excel export.
        shown_columns = columns[:8]
        table = document.add_table(rows=1, cols=len(shown_columns))
        table.style = "Table Grid"
        for cell, column in zip(table.rows[0].cells, shown_columns):
            cell.text = str(column)
        for row in rows[:20]:
            for cell, column in zip(table.add_row().cells, shown_columns):
                value = row.get(column) if isinstance(row, dict) else row[columns.index(column)]
                cell.text = "缺失" if value is None else str(value)
        if len(columns) > 8 or int(item.get("total_rows") or 0) > 20:
            document.add_paragraph("此处为部分行列预览，完整数据请使用同一成果的 Excel 导出。")


def _render_png(path: Path, charts: list[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for font_path in (
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ):
        if font_path.is_file():
            try:
                font_manager.fontManager.addfont(str(font_path))
                plt.rcParams["font.family"] = [font_manager.FontProperties(fname=str(font_path)).get_name()]
                break
            except (OSError, ValueError):
                continue
    plt.rcParams["axes.unicode_minus"] = False

    shown = [chart for chart in charts if chart.get("available")][:4]
    count = max(1, len(shown))
    columns = min(2, count)
    rows = math.ceil(count / columns)
    figure, axes = plt.subplots(rows, columns, figsize=(7 * columns, 4.5 * rows), constrained_layout=True, squeeze=False)
    if not shown:
        axes[0][0].text(0.5, 0.5, "当前没有适合绘图的数据", ha="center", va="center")
        axes[0][0].set_axis_off()
    for index, (axis, chart) in enumerate(zip(axes.flat, shown)):
        option = chart.get("option") or {}
        title = str(chart.get("title") or "Verified data")
        axis.set_title(title, wrap=True, fontsize=12)
        series = option.get("series") or []
        x_axis = option.get("xAxis") or {}
        y_axis = option.get("yAxis") or {}
        x_axis = x_axis[0] if isinstance(x_axis, list) and x_axis else x_axis
        y_axis = y_axis[0] if isinstance(y_axis, list) and y_axis else y_axis
        horizontal = isinstance(y_axis, dict) and y_axis.get("type") == "category"
        labels = (y_axis if horizontal else x_axis).get("data") or []
        if chart.get("type") in {"pie", "donut", "rose"}:
            data = series[0].get("data") or [] if series else []
            values = [float(item["value"]) for item in data if item.get("value") is not None]
            names = [str(item["name"])[:20] for item in data if item.get("value") is not None]
            if values and sum(abs(value) for value in values) > 0 and all(value >= 0 for value in values):
                axis.pie(values, labels=names, autopct="%1.1f%%")
            else:
                axis.text(0.5, 0.5, "构成图需要非负数值", ha="center", va="center")
            continue
        if chart.get("type") in {"scatter", "bubble", "connected_scatter"}:
            for current in series:
                points = [value.get("value") if isinstance(value, dict) else value
                          for value in current.get("data") or []]
                points = [value for value in points if isinstance(value, (tuple, list)) and len(value) >= 2
                          and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value[:2])]
                if points:
                    axis.scatter([value[0] for value in points], [value[1] for value in points], label=current.get("name"))
            axis.grid(alpha=0.2)
            continue
        supported = {"line", "area", "sparkline", "bar", "grouped_bar", "stacked_bar", "diverging_bar", "bar_line"}
        if chart.get("type") not in supported or not labels or not series:
            axis.text(0.5, 0.5, "此图形请在分析页面交互查看\n相关数据保留在明细中", ha="center", va="center")
            axis.set_axis_off()
            continue
        values_by_series = []
        for current in series:
            values = [value.get("value") if isinstance(value, dict) else value for value in current.get("data") or []]
            if len(values) != len(labels) or any(value is not None and not isinstance(value, (int, float)) for value in values):
                continue
            values_by_series.append((current, [float(value) if value is not None else math.nan for value in values]))
        if not values_by_series:
            axis.text(0.5, 0.5, "此图形请在分析页面查看", ha="center", va="center")
            axis.set_axis_off()
            continue
        positions = list(range(len(labels)))
        if chart.get("type") in {"line", "area", "sparkline"}:
            for current, values in values_by_series:
                axis.plot(positions, values, marker="o", label=current.get("name"))
        elif chart.get("type") == "bar_line" and len(values_by_series) >= 2:
            axis.bar(positions, values_by_series[0][1], color="#4f6bff")
            twin = axis.twinx()
            twin.plot(positions, values_by_series[1][1], color="#f97316", marker="o")
        elif chart.get("type") == "stacked_bar":
            bottom = [0.0] * len(positions)
            for current, values in values_by_series:
                if horizontal:
                    axis.barh(positions, values, left=bottom, label=current.get("name"))
                else:
                    axis.bar(positions, values, bottom=bottom, label=current.get("name"))
                bottom = [left + right for left, right in zip(bottom, values)]
        else:
            width = 0.8 / len(values_by_series)
            for index, (current, values) in enumerate(values_by_series):
                shifted = [value - 0.4 + width / 2 + index * width for value in positions]
                if horizontal:
                    axis.barh(shifted, values, height=width, label=current.get("name"))
                else:
                    axis.bar(shifted, values, width=width, label=current.get("name"))
        if len(values_by_series) > 1:
            axis.legend()
        if horizontal:
            axis.set_yticks(positions, [str(value)[:24] for value in labels])
        else:
            step = max(1, math.ceil(len(positions) / 10))
            shown_ticks = list(range(0, len(positions), step))
            if positions and positions[-1] not in shown_ticks:
                shown_ticks.append(positions[-1])
            axis.set_xticks(shown_ticks, [str(labels[index])[:24] for index in shown_ticks], rotation=30, ha="right")
        axis.grid(axis="y", alpha=0.2)
    for axis in list(axes.flat)[len(shown):]:
        axis.set_axis_off()
    figure.suptitle("分析数据看板", fontsize=16)
    figure.savefig(path, dpi=160, facecolor="white")
    plt.close(figure)


def generate_artifact(database: Database, run_id: str, workspace_id: str, kind: str) -> dict[str, Any]:
    if kind not in ARTIFACT_KINDS:
        raise ValueError("成果类型无效")
    publication, manifest = _published(database, run_id, workspace_id)
    existing = next((
        item for item in database.list("artifacts", workspace_id=workspace_id, limit=5000)
        if item.get("manifest_id") == manifest["id"] and item.get("kind") == kind and item.get("status") == "ready"
        and Path(str(item.get("path") or "")).is_file()
    ), None)
    if existing:
        return existing
    export_dir = current_app.config["SETTINGS"].export_dir
    base = f"{manifest['id']}_{manifest['version']}"
    payload = manifest["payload"]
    if kind == "report_html":
        path = export_dir / f"{base}_report.html"
        images = []
        available = [chart for chart in payload.get("charts") or [] if chart.get("available")]
        for offset in range(0, len(available), 4):
            image_path = export_dir / f"{base}_html_charts_{offset // 4}.png"
            _render_png(image_path, available[offset:offset + 4])
            images.append(base64.b64encode(image_path.read_bytes()).decode("ascii"))
        path.write_text(_render_html(payload, manifest, chart_images=images), encoding="utf-8")
    elif kind == "dashboard_png":
        path = export_dir / f"{base}_dashboard.png"
        _render_png(path, payload.get("charts") or [])
    elif kind == "data_xlsx":
        import pandas as pd

        path = export_dir / f"{base}_data.xlsx"
        tables = [item for item in payload.get("tables") or [] if item.get("result_id")]
        if not tables or any(item.get("completeness") != "complete" for item in tables):
            raise ValueError("已发布成果缺少完整的可导出明细")
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame([
                {"项目": "成果版本", "内容": str(manifest["version"])},
                {"项目": "结论", "内容": _safe_excel_text(str(payload.get("summary") or ""))},
                {"项目": "证据引用", "内容": ", ".join(payload.get("evidence_refs") or [])},
            ]).to_excel(writer, sheet_name="成果说明", index=False)
            for index, table in enumerate(tables):
                result = database.get("query_results", str(table["result_id"]), workspace_id=workspace_id)
                if not result or result.get("completeness") != "complete":
                    raise ValueError("成果明细不可用")
                if int(result.get("rows") or 0) > 1_048_575:
                    raise ValueError("明细行数超过 Excel 上限")
                frame = read_result_frame(result)
                frame.columns = [_safe_excel_text(str(column)) for column in frame.columns]
                for column in frame.select_dtypes(include=["object", "string"]).columns:
                    frame[column] = frame[column].map(_safe_excel_text)
                frame.to_excel(writer, sheet_name="验证数据" if index == 0 else f"验证数据 {index + 1}", index=False)
            for title, lines in _report_sections(payload):
                pd.DataFrame({"说明": [_safe_excel_text(line) for line in lines]}).to_excel(
                    writer, sheet_name=title, index=False,
                )
            for sheet in writer.book.worksheets:
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = sheet.dimensions
    elif kind == "report_pptx":
        path = export_dir / f"{base}_report.pptx"
        slides = Presentation()
        cover = slides.slides.add_slide(slides.slide_layouts[0])
        cover.shapes.title.text = str(payload.get("title") or "数据分析报告")
        cover.placeholders[1].text = f"成果版本 {manifest['version']} · {manifest['created_at']}"
        summary_lines = [str(payload.get("summary") or "")]
        if payload.get("kpis"):
            summary_lines.extend([
                f"{item.get('label')}：{_kpi_display(item)} {item.get('unit') or ''}"
                for item in payload["kpis"]
            ])
        _ppt_text(slides, "核心结论与指标", summary_lines)
        available = [chart for chart in payload.get("charts") or [] if chart.get("available")]
        for offset, chart in enumerate(available):
            png_path = export_dir / f"{base}_ppt_charts_{offset}.png"
            _render_png(png_path, [chart])
            chart_slide = slides.slides.add_slide(slides.slide_layouts[5])
            chart_slide.shapes.title.text = "数据看板"
            picture = chart_slide.shapes.add_picture(str(png_path), PptInches(0.5), PptInches(1.4), width=PptInches(9))
            if picture.height > PptInches(5.5):
                scale = PptInches(5.5) / picture.height
                picture.width = int(picture.width * scale)
                picture.height = PptInches(5.5)
            picture.left = int((slides.slide_width - picture.width) / 2)
        grouped = {"发现与行动": [], "范围与分析说明": [], "指标口径": []}
        for title, lines in _report_sections(payload):
            group = ("发现与行动" if title in {"发现与解释", "行动建议"}
                     else "指标口径" if title == "指标与字段说明" else "范围与分析说明")
            grouped[group].extend(f"{title}：{line}" if group != "指标口径" else line for line in lines)
        for title, lines in grouped.items():
            _ppt_text(slides, title, lines)
        lines = [
            "相关数据：" + "、".join(str(table.get("title") or "分析明细") for table in payload.get("tables") or []),
            "完整查询证据可在分析页面查看。",
            *(str(item) for item in payload.get("limitations") or []),
        ]
        _ppt_text(slides, "证据与限制", lines)
        for slide in slides.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(26) if shape == slide.shapes.title else Pt(18)
                            run.font.name = "Microsoft YaHei"
        slides.save(path)
    else:
        path = export_dir / f"{base}_{'summary' if kind == 'summary_docx' else 'report'}.docx"
        document = Document()
        current = _doc_header(
            document, manifest,
            "分析结论摘要" if kind == "summary_docx" else str(payload.get("title") or "数据分析报告"),
        )
        document.add_heading("经验证结论", level=1)
        document.add_paragraph(str(current.get("summary") or ""))
        _write_kpis(document, current.get("kpis") or [])
        claim_details = current.get("claim_details") or []
        if claim_details and not (current.get("report") or {}).get("findings"):
            document.add_heading("结论依据", level=1)
            for claim in claim_details:
                document.add_paragraph(str(claim.get("text") or ""), style="List Bullet")
        if kind == "report_docx":
            document.add_heading("数据看板", level=1)
            available = [chart for chart in current.get("charts") or [] if chart.get("available")]
            if available:
                for offset, chart in enumerate(available):
                    png_path = export_dir / f"{base}_word_charts_{offset}.png"
                    _render_png(png_path, [chart])
                    document.add_picture(str(png_path), width=Inches(6))
            else:
                document.add_paragraph("当前已验证数据没有适合绘图的维度和数值列。")
            for title, lines in _report_sections(current):
                document.add_heading(title, level=1)
                for line in lines:
                    document.add_paragraph(line, style="List Bullet")
            document.add_heading("数据明细", level=1)
            _write_data_preview(document, current.get("tables") or [])
        document.add_heading("限制与待核对事项", level=1)
        for value in current.get("limitations") or ["无"]:
            document.add_paragraph(str(value), style="List Bullet")
        document.save(path)
    return _record_artifact(database, path, kind, workspace_id, run_id, publication, manifest)


def _escape(value: object) -> str:
    """Escape untrusted text for HTML output.

    Source data, metric labels and model prose all end up in this document, so
    every interpolation goes through the same escaper.
    """
    return html.escape(str(value if value is not None else ""), quote=True)


def _render_html(payload: dict[str, Any], manifest: dict[str, Any], *, chart_images: list[str] | None = None) -> str:
    current = payload.get("current") or payload
    parts: list[str] = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{_escape(payload.get('title') or '数据分析报告')}</title>",
        "<style>",
        "body{margin:0;padding:40px 24px;font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;",
        "color:#161B26;background:#F6F8FC}",
        "main{max-width:860px;margin:0 auto;background:#fff;border:1px solid #E5E9F0;border-radius:12px;padding:40px}",
        "h1{font-size:26px;margin:0 0 8px}h2{font-size:18px;margin:32px 0 12px}",
        "h3{font-size:15px;margin:20px 0 8px}",
        "p,li,td,th{overflow-wrap:anywhere}img{max-width:100%;height:auto}",
        ".table-wrap{overflow-x:auto}summary{cursor:pointer}",
        "@media(max-width:600px){body{padding:12px}main{padding:20px}}",
        ".meta{color:#5F697B;font-size:13px;margin-bottom:24px}",
        ".kpis{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}",
        ".kpi{flex:1 1 180px;border:1px solid #E5E9F0;border-radius:10px;padding:14px 16px}",
        ".kpi b{display:block;font-size:24px;margin-top:4px}",
        ".kpi span{color:#8D96A8;font-size:12px}",
        "table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}",
        "th,td{border:1px solid #E5E9F0;padding:8px 10px;text-align:left}",
        "th{background:#F8FAFD;font-weight:600}",
        "ul{padding-left:20px}.note{color:#5F697B;font-size:13px;background:#F8FAFD;",
        "border-left:3px solid #4F6BFF;padding:12px 16px;border-radius:0 8px 8px 0}",
        "</style></head><body><main>",
        f"<h1>{_escape(payload.get('title') or '数据分析报告')}</h1>",
        f"<p class=\"meta\">成果版本 {_escape(manifest.get('version'))} · "
        f"生成时间 {_escape(manifest.get('created_at'))}</p>",
    ]
    contract = payload.get("contract") or {}
    if contract.get("objective"):
        parts.append(f"<h2>分析目标</h2><p>{_escape(contract['objective'])}</p>")
    if contract.get("coverage"):
        parts.append(f"<p class=\"note\">统计范围：{_escape(contract['coverage'])}</p>")

    kpis = current.get("kpis") or []
    if kpis:
        cells = "".join(
            f"<div class=\"kpi\"><span>{_escape(item.get('label') or item.get('name') or '指标')}</span>"
            f"<b>{_escape(_kpi_display(item))}</b>"
            f"<span>{_escape(item.get('unit') or '')}</span></div>"
            for item in kpis
        )
        parts.append(f"<h2>核心指标</h2><div class=\"kpis\">{cells}</div>")

    summary = current.get("summary") or payload.get("summary")
    if summary:
        parts.append(f"<h2>结论</h2><p>{_escape(summary)}</p>")
    for image_data in chart_images or []:
        parts.append(f'<figure><img alt="分析数据看板" src="data:image/png;base64,{image_data}"></figure>')
    for title, lines in _report_sections(payload):
        parts.append(f"<h2>{_escape(title)}</h2><ul>"
                     + "".join(f"<li>{_escape(line)}</li>" for line in lines) + "</ul>")

    for table in payload.get("tables") or []:
        if not table.get("columns") or not table.get("rows"):
            continue
        header = "".join(f"<th>{_escape(column)}</th>" for column in table["columns"])
        body = "".join(
            "<tr>" + "".join(
                f"<td>{_escape('缺失' if cell is None else cell)}</td>" for cell in (
                    row.get(column) if isinstance(row, dict) else row[index]
                    for index, column in enumerate(table["columns"])
                )
            ) + "</tr>"
            for row in table["rows"][:200]
        )
        parts.append(
            f"<h3>{_escape(table.get('title') or '数据明细')}</h3>"
            f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'
        )
        if int(table.get("total_rows") or 0) > len(table["rows"]):
            parts.append('<p class="note">此处为部分记录预览，完整数据请使用同一成果的 Excel 导出。</p>')

    limitations = current.get("limitations") or payload.get("limitations") or []
    if limitations:
        items = "".join(f"<li>{_escape(value)}</li>" for value in limitations)
        parts.append(f"<h2>限制与待核对事项</h2><ul>{items}</ul>")
    parts.append(
        "<h2>数据引用</h2><p class=\"meta\">"
        + _escape(", ".join(str(item) for item in (payload.get("evidence_refs") or [])) or "无")
        + "</p></main></body></html>"
    )
    return "".join(parts)


def generate_artifacts(database: Database, run_id: str, workspace_id: str, kinds: list[str] | None = None) -> list[dict]:
    requested = list(dict.fromkeys(kinds or DEFAULT_ARTIFACT_KINDS))
    return [generate_artifact(database, run_id, workspace_id, kind) for kind in requested]


def _recipients(value: str | list[str]) -> list[str]:
    raw = ",".join(value) if isinstance(value, list) else str(value or "")
    addresses = [address for _name, address in getaddresses([raw.replace("，", ",")]) if address]
    pattern = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
    if not addresses or len(addresses) > 50 or any(not pattern.fullmatch(item) for item in addresses):
        raise ValueError("收件人必须是有效的逗号分隔邮箱，最多 50 个")
    return list(dict.fromkeys(addresses))


def _email_message(
    *, sender: str, recipients: list[str], subject: str, text: str, artifacts: list[dict],
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject[:200]
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    for artifact in artifacts:
        path = Path(artifact["path"])
        subtype = "vnd.openxmlformats-officedocument.wordprocessingml.document" if path.suffix == ".docx" else "png"
        maintype = "application" if path.suffix == ".docx" else "image"
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=artifact["filename"])
    return message


def prepare_eml(
    database: Database, run_id: str, workspace_id: str, *, recipients: str | list[str],
    subject: str, text: str, kinds: list[str] | None = None, sender: str = "analysis@localhost",
) -> tuple[dict, list[dict]]:
    publication, manifest = _published(database, run_id, workspace_id)
    addresses = _recipients(recipients)
    artifacts = generate_artifacts(database, run_id, workspace_id, kinds)
    message = _email_message(
        sender=sender, recipients=addresses, subject=subject, text=text, artifacts=artifacts,
    )
    signature = hashlib.sha256((
        manifest["id"] + "\0" + ",".join(addresses) + "\0" + subject + "\0" +
        "\0".join(item["sha256"] for item in artifacts)
    ).encode("utf-8")).hexdigest()[:20]
    path = current_app.config["SETTINGS"].export_dir / f"{manifest['id']}_{signature}.eml"
    path.write_bytes(message.as_bytes())
    eml = _record_artifact(database, path, "email_eml", workspace_id, run_id, publication, manifest)
    eml["recipients"] = addresses
    return eml, artifacts


def send_email(
    database: Database, run_id: str, workspace_id: str, *, connector: dict,
    recipients: str | list[str], subject: str, text: str, kinds: list[str] | None,
    idempotency_key: str,
) -> dict[str, Any]:
    if connector.get("type") != "email" or not connector.get("enabled", True):
        raise ValueError("请选择已启用的 SMTP 邮件连接器")
    if not idempotency_key:
        raise ValueError("邮件发送必须提供 Idempotency-Key")
    existing = next((
        item for item in database.list("email_deliveries", workspace_id=workspace_id, limit=5000)
        if item.get("idempotency_key") == idempotency_key and item.get("run_id") == run_id
    ), None)
    if existing:
        return existing
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(connector.get("credential", ""), {})
    sender = str(secret.get("sender") or secret.get("username") or "")
    if not sender or not secret.get("host"):
        raise ValueError("SMTP 连接器缺少 host/sender")
    eml, artifacts = prepare_eml(
        database, run_id, workspace_id, recipients=recipients, subject=subject, text=text,
        kinds=kinds, sender=sender,
    )
    addresses = eml["recipients"]
    delivery = database.put("email_deliveries", {
        "id": database.new_id("mail"), "workspace_id": workspace_id, "run_id": run_id,
        "publication_id": eml["publication_id"], "manifest_id": eml["manifest_id"],
        "connector_id": connector["id"], "idempotency_key": idempotency_key,
        "status": "sending", "recipients": addresses, "subject": subject,
        "artifact_ids": [item["id"] for item in artifacts],
        "artifact_hashes": {item["id"]: item["sha256"] for item in artifacts},
        "eml_artifact_id": eml["id"], "started_at": utcnow(),
    }, workspace_id=workspace_id)
    message = _email_message(sender=sender, recipients=addresses, subject=subject, text=text, artifacts=artifacts)
    try:
        if secret.get("use_ssl"):
            client = smtplib.SMTP_SSL(secret["host"], int(secret.get("port", 465)), timeout=30, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(secret["host"], int(secret.get("port", 587)), timeout=30)
        with client as smtp:
            if secret.get("use_tls", True) and not secret.get("use_ssl"):
                smtp.starttls(context=ssl.create_default_context())
            if secret.get("username"):
                smtp.login(secret["username"], secret.get("password", ""))
            refused = smtp.send_message(message, to_addrs=addresses)
        status = "sent" if not refused else "partial"
        return database.patch("email_deliveries", delivery["id"], {
            "status": status, "refused": refused, "finished_at": utcnow(),
        }, workspace_id=workspace_id) or delivery
    except Exception as exc:
        # Network failure after DATA can have an unknown delivery outcome. Idempotency
        # prevents automatic resend; a user may explicitly choose a new key.
        database.patch("email_deliveries", delivery["id"], {
            "status": "unknown", "error": str(exc), "finished_at": utcnow(),
        }, workspace_id=workspace_id)
        raise ConnectionError("邮件发送状态未知，系统不会使用同一幂等键自动重发") from exc
