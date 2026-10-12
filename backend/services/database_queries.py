"""Native read-only SQL execution with admission, cost and transfer budgets."""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable

import pandas as pd
import sqlglot
from sqlglot import exp
from flask import current_app


def limits() -> dict:
    return {
        "concurrency": max(1, min(int(os.getenv("MERIDIAN_DATABASE_QUERY_CONCURRENCY", "4")), 32)),
        "queue_seconds": max(0, min(int(os.getenv("MERIDIAN_DATABASE_QUERY_QUEUE_SECONDS", "10")), 60)),
        "scan_rows": max(1, int(os.getenv("MERIDIAN_DATABASE_MAX_SCAN_ROWS", "50000000"))),
        "scan_bytes": max(1, int(os.getenv("MERIDIAN_DATABASE_MAX_SCAN_MB", "2048"))) * 1024 * 1024,
    }


@contextmanager
def admission(source: dict, should_cancel: Callable[[], bool] | None = None):
    application = current_app._get_current_object()
    guard = application.extensions.setdefault("database_query_lock", threading.Lock())
    with guard:
        gates = application.extensions.setdefault("database_query_gates", {})
        endpoint = f"{source.get('endpoint')}:{source.get('port')}:{source.get('driver')}"
        semaphore = gates.setdefault(endpoint, threading.BoundedSemaphore(limits()["concurrency"]))
    deadline = time.monotonic() + limits()["queue_seconds"]
    acquired = False
    while not acquired:
        if should_cancel and should_cancel():
            raise InterruptedError("查询已取消")
        acquired = semaphore.acquire(timeout=0.1)
        if not acquired and time.monotonic() >= deadline:
            raise ValueError("当前数据库查询较多，请稍后重试或缩小查询范围")
    try:
        yield
    finally:
        semaphore.release()


def estimate_query(connection, sql: str, source: dict) -> dict:
    dialect = connection.engine.dialect.name
    family = source.get("engine_family", dialect)
    sql_dialect = family if family in {"doris", "starrocks"} else {"postgresql": "postgres", "mssql": "tsql"}.get(dialect, dialect)
    root = sqlglot.parse_one(sql, read=sql_dialect)
    for join in root.find_all(exp.Join):
        if not join.args.get("on") and not join.args.get("using"):
            raise ValueError("关联查询缺少关联条件，请指定业务关联字段")
    estimate = {"engine": family, "estimated_scan_rows": None, "estimated_scan_bytes": None,
                "status": "unknown", "warnings": []}
    bound = root.args.get("limit")
    preview = (isinstance(root, exp.Select) and bound is not None
               and isinstance(bound.expression, exp.Literal) and not bound.expression.is_string
               and not any(root.args.get(key) for key in ("where", "having", "qualify", "order", "group", "distinct", "offset", "operation_modifiers"))
               and not any(root.find(kind) for kind in (exp.AggFunc, exp.Window, exp.Join, exp.Subquery, exp.CTE)))
    if preview:
        return {**estimate, "status": "bounded_preview", "estimated_scan_rows": int(bound.expression.this)}
    if not list(root.find_all(exp.Table)):
        return {**estimate, "status": "available", "estimated_scan_rows": 0}
    if dialect == "sqlite":
        connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, execution_options={"no_parameters": True}).all()
        return {**estimate, "status": "bounded_local"}
    if family in {"doris", "starrocks"}:
        rows = connection.exec_driver_sql("EXPLAIN " + sql, execution_options={"no_parameters": True}).all()
        plan = "\n".join(str(row[0]) for row in rows)
        scans = re.split(r"(?i)(?=\d+:V?OlapScanNode|\d+:V?ScanNode)", plan)
        cardinalities = []
        partition_counts = []
        for scan in scans[1:]:
            value = re.search(r"cardinality\s*=\s*([\d,]+)", scan, re.I)
            if value:
                predicted = int(value[1].replace(",", ""))
                # A scan node's output cardinality may already include filter
                # selectivity. Use partition/tablet-pruned metadata as a
                # conservative input scan budget, not the aggregate's row count.
                table_match = re.search(r"TABLE:\s*(?:[^\s.(]+\.)?([^\s.(]+)", scan, re.I)
                metadata = next((table for table in source.get("tables") or []
                                 if table_match and table["name"].lower() == table_match[1].strip('`').lower()), None)
                if metadata and metadata.get("rows") is not None:
                    ratios = [1.0]
                    for label in ("partitions", "tablets"):
                        pruning = re.search(rf"{label}\s*=\s*(\d+)\s*/\s*(\d+)", scan, re.I)
                        if pruning and int(pruning[2]):
                            ratios.append(int(pruning[1]) / int(pruning[2]))
                    predicted = max(predicted, int(int(metadata["rows"]) * min(ratios)))
                cardinalities.append(predicted)
            partition_counts.extend(re.findall(r"partitions\s*=\s*(\d+)\s*/\s*(\d+)", scan, re.I))
        if cardinalities:
            estimate.update(status="available", estimated_scan_rows=sum(cardinalities))
        if partition_counts and all(int(used) == int(total) for used, total in partition_counts):
            estimate["warnings"].append("当前查询未产生分区裁剪，请关注日期范围和扫描成本")
        estimate["plan"] = plan[:12000]
    elif dialect == "mysql":
        raw = connection.exec_driver_sql("EXPLAIN FORMAT=JSON " + sql, execution_options={"no_parameters": True}).scalar()
        plan = json.loads(raw)
        counts = []
        def visit(value):
            if isinstance(value, dict):
                if "rows_examined_per_scan" in value:
                    counts.append(int(value["rows_examined_per_scan"]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(plan)
        if counts:
            estimate.update(status="available", estimated_scan_rows=sum(counts))
    elif dialect == "postgresql":
        raw = connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + sql, execution_options={"no_parameters": True}).scalar()
        plan = json.loads(raw) if isinstance(raw, str) else raw
        counts = []
        def visit(value):
            if isinstance(value, dict):
                if "Scan" in str(value.get("Node Type", "")):
                    counts.append(int(value.get("Plan Rows", 0)))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(plan)
        if counts:
            estimate.update(status="available", estimated_scan_rows=sum(counts))
    if estimate["status"] == "unknown":
        estimate["warnings"].append("扫描量预估不可用，本次查询由数据库超时及并发上限控制")
    scan_rows = estimate["estimated_scan_rows"]
    if scan_rows is not None and scan_rows > limits()["scan_rows"]:
        raise ValueError(f"预计扫描约 {scan_rows:,} 行，超过单次查询预算；请缩小时间范围或先按业务维度汇总")
    # Metadata byte counts are conservative when the plan cannot report bytes.
    # Only use them for unfiltered scans, avoiding rejection of pruned queries.
    if not root.find(exp.Where):
        names = {table.name.lower() for table in root.find_all(exp.Table)}
        sizes = [int(table["data_bytes"]) for table in source.get("tables") or []
                 if table["name"].lower() in names and table.get("data_bytes") is not None]
        if sizes:
            estimate["estimated_scan_bytes"] = sum(sizes)
            if sum(sizes) > limits()["scan_bytes"]:
                raise ValueError("无筛选查询的数据规模超过扫描预算，请增加时间或业务范围条件")
    return estimate


def _interrupt(connection) -> None:
    """Interrupt client work without issuing source DDL, DML or CTAS."""
    try:
        raw = connection.connection.driver_connection
        if connection.engine.dialect.name == "sqlite":
            raw.interrupt()
        elif hasattr(raw, "cancel"):
            raw.cancel()
        elif getattr(raw, "_sock", None):
            raw._sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass


@contextmanager
def watch_query(connection, timeout_seconds: int, should_cancel: Callable[[], bool] | None = None):
    done = threading.Event()
    state = {"cancelled": False, "timed_out": False}
    deadline = time.monotonic() + timeout_seconds
    def watch():
        while not done.wait(0.1):
            cancelled = bool(should_cancel and should_cancel())
            expired = time.monotonic() >= deadline
            if cancelled or expired:
                state.update(cancelled=cancelled, timed_out=expired and not cancelled)
                _interrupt(connection)
                return
    watcher = threading.Thread(target=watch, daemon=True, name="database-query-watch")
    watcher.start()
    try:
        yield state
        if state["cancelled"]:
            raise InterruptedError("查询已取消")
        if state["timed_out"]:
            raise ValueError("查询超过时间限制，请缩小查询范围")
    except Exception as exc:
        if state["cancelled"]:
            connection.invalidate()
            raise InterruptedError("查询已取消") from exc
        if state["timed_out"]:
            connection.invalidate()
            raise ValueError("查询超过时间限制，请缩小查询范围") from exc
        raise
    finally:
        done.set()
        watcher.join(timeout=1)


def read_bounded(connection, sql: str, *, max_rows: int, max_bytes: int, max_cell_bytes: int,
                 should_cancel: Callable[[], bool] | None = None) -> tuple[pd.DataFrame, int]:
    """Use server-side cursors; reject bytes before constructing the DataFrame."""
    raw = connection.connection.driver_connection
    original_read = getattr(raw, "_read_bytes", None)
    wire_bytes = 0
    if original_read is not None:
        def budgeted_read(size):
            nonlocal wire_bytes
            if wire_bytes + size > max_bytes:
                raise ValueError("数据库返回数据超过字节预算，请减少字段或先汇总")
            data = original_read(size)
            wire_bytes += len(data)
            return data
        raw._read_bytes = budgeted_read
    result = None
    records = []
    received = 0
    try:
        result = connection.execution_options(stream_results=True, max_row_buffer=1, no_parameters=True).exec_driver_sql(sql)
        names = list(result.keys())
        while len(records) < max_rows:
            if should_cancel and should_cancel():
                raise InterruptedError("查询已取消")
            batch = result.fetchmany(1)
            if not batch:
                break
            for row in batch:
                record = tuple(row)
                row_bytes = sys.getsizeof(record)
                for value in record:
                    if isinstance(value, (str, bytes, bytearray)):
                        value_bytes = len(value.encode("utf-8")) if isinstance(value, str) else len(value)
                        if value_bytes > max_cell_bytes:
                            raise ValueError("查询字段内容超过大小上限，请仅选择分析所需字段")
                        row_bytes += max(value_bytes, sys.getsizeof(value))
                    else:
                        row_bytes += sys.getsizeof(value)
                received += row_bytes
                if received > max_bytes:
                    raise ValueError("查询结果超过字节预算，请减少字段、缩小范围或先汇总")
                records.append(record)
        return pd.DataFrame.from_records(records, columns=names), received
    except Exception:
        # Closing an unbuffered cursor may drain remaining rows. Invalidate on
        # budget/cancel failures instead, so an oversized stream is never drained.
        _interrupt(connection)
        connection.invalidate()
        raise
    finally:
        if original_read is not None:
            raw._read_bytes = original_read
        if result is not None and not connection.invalidated:
            result.close()
