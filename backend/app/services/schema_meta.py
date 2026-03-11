"""
表头码表生成器 — 从 DuckDB (Lance 表) 提取字段信息 + 示例值

用途：
- 为 LLM NL2SQL 提供精确的 schema 描述
- 包含字段名、类型、中文含义、示例值/枚举值
- 结果缓存，避免重复查询
"""

import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

# ── 字段中文含义映射 ──────────────────────────────────────────────
# 可以通过配置文件或数据库动态加载
COLUMN_DESCRIPTIONS: Dict[str, str] = {}

# 枚举字段 — 这些字段用 SELECT DISTINCT 获取全部值而非采样
ENUM_FIELDS: set = set()

# 表中文名映射
TABLE_DESCRIPTIONS: Dict[str, str] = {}

# 需要关注的表（白名单）
TARGET_TABLES: List[str] = []


def load_schema_config(config: Dict[str, Any]) -> None:
    """
    从配置加载 schema 元数据配置

    Args:
        config: 包含 schema 配置的字典
    """
    global COLUMN_DESCRIPTIONS, ENUM_FIELDS, TABLE_DESCRIPTIONS, TARGET_TABLES

    schema_cfg = config.get("schema", {})

    # 加载字段中文映射
    COLUMN_DESCRIPTIONS = schema_cfg.get("column_descriptions", {})

    # 加载枚举字段
    enum_fields = schema_cfg.get("enum_fields", [])
    ENUM_FIELDS = set(enum_fields) if enum_fields else set()

    # 加载表中文名
    TABLE_DESCRIPTIONS = schema_cfg.get("table_descriptions", {})

    # 加载目标表
    TARGET_TABLES = schema_cfg.get("target_tables", [])


def get_column_description(column_name: str) -> str:
    """获取字段的中文描述"""
    return COLUMN_DESCRIPTIONS.get(column_name, "")


def is_enum_field(column_name: str) -> bool:
    """判断是否为枚举字段"""
    return column_name in ENUM_FIELDS


def get_table_description(table_name: str) -> str:
    """获取表的中文描述"""
    return TABLE_DESCRIPTIONS.get(table_name, table_name)


def _get_table_columns(engine, table: str) -> List[Tuple[str, str]]:
    """获取表的列名和类型（从 DuckDB information_schema）"""
    conn = getattr(engine, "conn", None) or getattr(engine, "con", None)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ?", [table]
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        # 尝试使用 PRAGMA
        try:
            rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            return [(r[1], r[2]) for r in rows]
        except Exception:
            return []


def _get_sample_values(engine, table: str, column: str,
                       is_enum: bool = False, limit: int = 5) -> str:
    """获取字段的示例值或枚举值"""
    def _quote_ident(name: str) -> str:
        return '"' + (name or "").replace('"', '""') + '"'

    conn = getattr(engine, "conn", None) or getattr(engine, "con", None)
    if conn is None:
        return ""

    col_ident = _quote_ident(column)
    table_ident = _quote_ident(table)
    try:
        if is_enum:
            rows = conn.execute(
                f"SELECT DISTINCT {col_ident} FROM {table_ident} "
                f"WHERE {col_ident} IS NOT NULL AND CAST({col_ident} AS VARCHAR) <> '' "
                f"ORDER BY {col_ident}"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT DISTINCT {col_ident} FROM {table_ident} "
                f"WHERE {col_ident} IS NOT NULL AND CAST({col_ident} AS VARCHAR) <> '' "
                f"ORDER BY {col_ident} LIMIT {limit}"
            ).fetchall()
        values = [str(r[0]) for r in rows if r[0] is not None]
        if not values:
            return ""
        truncated = []
        for v in values:
            if len(v) > 30:
                truncated.append(v[:27] + "...")
            else:
                truncated.append(v)
        return ", ".join(truncated)
    except Exception:
        return ""


def _build_table_prompt(engine, table: str) -> str:
    """为单个表生成码表文本"""
    table_desc = get_table_description(table)
    columns = _get_table_columns(engine, table)

    if not columns:
        return ""

    lines = [
        f"## 表: {table} ({table_desc})",
        "| 列名 | 类型 | 中文含义 | 示例值 |",
        "|------|------|----------|--------|",
    ]

    for col_name, col_type in columns:
        desc = get_column_description(col_name)
        is_enum = is_enum_field(col_name)
        samples = _get_sample_values(engine, table, col_name, is_enum=is_enum)
        lines.append(f"| {col_name} | {col_type} | {desc} | {samples} |")

    return "\n".join(lines)


def build_schema_prompt(db_path: str, engine=None) -> str:
    """
    从 DuckDB (Lance 表) 生成完整的 schema 码表文本。

    Args:
        db_path: DuckDB 数据库路径或 LanceDB 目录路径
        engine: 可选的 DuckDB 引擎实例

    Returns:
        Markdown 格式的码表描述字符串
    """
    from app.orchestrator_duckdb import get_engine

    # 如果没有传入 engine，则获取一个
    if engine is None:
        eng = get_engine()
        eng.connect()
        engine = eng

    # 如果指定了目标表
    tables_to_process = TARGET_TABLES if TARGET_TABLES else engine.get_tables()

    parts = []
    for table in tables_to_process:
        try:
            # 跳过系统表
            if table.startswith("_") or table.startswith("sqlite_"):
                continue
            cols = _get_table_columns(engine, table)
            if cols:
                prompt = _build_table_prompt(engine, table)
                if prompt:
                    parts.append(prompt)
        except Exception:
            pass

    return "\n\n".join(parts)


@lru_cache(maxsize=4)
def build_schema_prompt_cached(db_path: str) -> str:
    """
    带缓存的 schema 提示生成（用于 LLM 调用）
    """
    return build_schema_prompt(db_path)


def get_table_schema(db_path: str, table_name: str) -> List[Dict[str, str]]:
    """
    获取指定表的 schema 信息

    Args:
        db_path: 数据库路径
        table_name: 表名

    Returns:
        字段列表，每项包含 column_name, column_type, description
    """
    from app.orchestrator_duckdb import get_engine

    eng = get_engine()
    eng.connect()

    columns = _get_table_columns(eng, table_name)
    result = []
    for col_name, col_type in columns:
        result.append({
            "column_name": col_name,
            "column_type": col_type,
            "description": get_column_description(col_name),
            "is_enum": is_enum_field(col_name),
        })

    return result
