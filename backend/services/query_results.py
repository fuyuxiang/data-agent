"""Read bounded query evidence without re-inferring its business values."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import math
import re

import pandas as pd
from flask import current_app, has_app_context


def normalize_result_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Give every returned field a stable key without overwriting duplicate names."""
    original = [str(column) for column in frame.columns]
    reserved, used, columns = set(original), set(), []
    for name in original:
        candidate, suffix = name, 2
        if candidate in used:
            candidate = f"{name}_{suffix}"
            while candidate in reserved or candidate in used:
                suffix += 1
                candidate = f"{name}_{suffix}"
        used.add(candidate)
        columns.append(candidate)
    if columns != list(frame.columns):
        frame = frame.copy()
        frame.columns = columns
    return frame


def write_result_frame(frame: pd.DataFrame, path: Path) -> Path:
    """Keep typed results; mixed SQLite object columns use exact JSON records."""
    import pyarrow as arrow

    frame = _finite_frame(frame)
    try:
        frame.to_parquet(path, index=False)
        return path
    except (arrow.ArrowInvalid, arrow.ArrowTypeError, arrow.ArrowNotImplementedError):
        from .datasets import frame_records

        path.unlink(missing_ok=True)
        target = path.with_suffix(".json")
        target.write_text(json.dumps({"columns": list(frame.columns), "data": frame_records(frame, len(frame))},
                                     ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return target


def _restore_legacy_csv_values(frame: pd.DataFrame, result: dict[str, Any]) -> pd.DataFrame:
    """Use preview types without coercing later mixed fields or business codes."""
    records = result.get("data") or result.get("preview") or []
    for column in frame.columns:
        values = [row.get(column) for row in records if isinstance(row, dict) and row.get(column) is not None]
        numeric = any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
        boolean = any(isinstance(value, bool) for value in values)
        if numeric or boolean:
            frame[column] = pd.Series(
                [_canonical_csv_value(value, numeric=numeric, boolean=boolean) for value in frame[column]],
                index=frame.index, dtype=object,
            )
    return frame


def _canonical_csv_value(value: Any, *, numeric: bool, boolean: bool) -> Any:
    if not isinstance(value, str):
        return None if pd.isna(value) else value
    if boolean and value in {"True", "False"}:
        return value == "True"
    if numeric:
        if value in {"inf", "-inf", "Infinity", "-Infinity"}:
            return None
        if re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
            try:
                integer = int(value)
            except ValueError:
                pass
            else:
                if str(integer) == value:
                    return integer
        try:
            number = float(value)
        except ValueError:
            pass
        else:
            if math.isfinite(number) and str(number) == value:
                return number
    return value


def _restore_json_dtypes(frame: pd.DataFrame, records: list[dict]) -> pd.DataFrame:
    # Inspect the complete stored column, so paging through a mixed column never
    # changes its type just because a page happens to contain only numbers.
    for column in frame.columns:
        values = [row.get(column) for row in records if row.get(column) is not None]
        if not values:
            continue
        if all(isinstance(value, bool) for value in values):
            frame[column] = frame[column].astype("boolean")
        elif all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            if -(2**63) <= min(values) and max(values) <= 2**63 - 1:
                frame[column] = frame[column].astype("Int64")
            elif min(values) >= 0 and max(values) <= 2**64 - 1:
                frame[column] = frame[column].astype("UInt64")
        elif all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            frame[column] = frame[column].astype("Float64")
    return frame


def _finite_frame(frame: pd.DataFrame) -> pd.DataFrame:
    copied = False
    for column in frame.select_dtypes(include=["number", "object"]).columns:
        if pd.api.types.is_object_dtype(frame[column].dtype):
            invalid = frame[column].map(
                lambda value: pd.api.types.is_float(value) and not math.isfinite(value),
            )
        else:
            invalid = frame[column].isin([math.inf, -math.inf])
        if invalid.any():
            if not copied:
                frame = frame.copy()
                copied = True
            frame[column] = frame[column].mask(invalid, None)
    return frame


def read_result_frame(result: dict[str, Any], *, offset: int = 0, limit: int | None = None) -> pd.DataFrame:
    """Support typed Parquet results and existing CSV evidence, with bounded pages."""
    return _finite_frame(_read_result_frame(result, offset=offset, limit=limit))


def _read_result_frame(result: dict[str, Any], *, offset: int = 0, limit: int | None = None) -> pd.DataFrame:
    start = max(0, int(offset))
    size = None if limit is None else max(0, int(limit))
    filename = str(result.get("path") or "")
    if not filename:
        records = result.get("data") or result.get("preview") or []
        if int(result.get("rows") or 0) > len(records):
            raise FileNotFoundError("完整明细文件不存在，请重新运行分析")
        return pd.DataFrame(records, columns=result.get("columns") or None).iloc[start:None if size is None else start + size]
    path = Path(filename)
    export_root = current_app.config["SETTINGS"].export_dir.resolve() if has_app_context() else None
    if path.is_symlink() or (export_root is not None and export_root not in path.resolve().parents):
        raise PermissionError("成果明细路径无效")
    if not path.is_file():
        raise FileNotFoundError("完整明细文件不存在，请重新运行分析")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("明细结果超过本地读取上限，请在仓内缩小结果范围")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload["data"][start:None if size is None else start + size]
        return _restore_json_dtypes(pd.DataFrame(records, columns=payload["columns"], dtype=object), payload["data"])
    if path.suffix.lower() == ".parquet":
        if size is None:
            return pd.read_parquet(path).iloc[start:]
        import pyarrow.parquet as parquet

        file = parquet.ParquetFile(path)
        batches, position, remaining = [], 0, size
        for batch in file.iter_batches(batch_size=max(1, min(size, 500))):
            if remaining <= 0:
                break
            if position + batch.num_rows > start:
                begin = max(0, start - position)
                part = batch.slice(begin, min(batch.num_rows - begin, remaining))
                batches.append(part.to_pandas())
                remaining -= part.num_rows
            position += batch.num_rows
        if batches:
            return pd.concat(batches, ignore_index=True)
        import pyarrow as arrow

        return arrow.Table.from_batches([], schema=file.schema_arrow).to_pandas()
    if path.suffix.lower() != ".csv":
        raise ValueError("不支持的本地结果格式")
    # skiprows counts physical lines, which corrupts pages containing quoted
    # multiline fields. Chunking advances by actual CSV records instead.
    chunks, position, remaining = [], 0, size
    with pd.read_csv(
        path, chunksize=500, dtype="string", keep_default_na=False,
        na_values=[""], skip_blank_lines=False,
    ) as reader:
        for chunk in reader:
            if remaining is not None and remaining <= 0:
                break
            if position + len(chunk) > start:
                begin = max(0, start - position)
                part = chunk.iloc[begin:] if remaining is None else chunk.iloc[begin:begin + remaining]
                chunks.append(part)
                if remaining is not None:
                    remaining -= len(part)
            position += len(chunk)
    if chunks:
        frame = _restore_legacy_csv_values(pd.concat(chunks, ignore_index=True).astype(object), result)
        # CSV cannot distinguish empty strings from null. Restore cells whose
        # exact values were retained in the original preview.
        records = result.get("data") or result.get("preview") or []
        for index, row in enumerate(records[start:start + len(frame)]):
            if isinstance(row, dict):
                for column in frame.columns:
                    if column in row:
                        frame.at[index, column] = row[column]
        typed_records = frame.where(frame.notna(), None).to_dict(orient="records")
        return _restore_json_dtypes(frame, typed_records)
    return pd.DataFrame(columns=result.get("columns") or [])
