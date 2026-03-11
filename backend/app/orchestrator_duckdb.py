"""
DuckDB 执行引擎 - 增强版

功能：
1. 支持 DuckDB 文件和 LanceDB
2. 视图创建
3. 线程安全
4. CSV 文件加载
"""

import os
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from app.orchestrator_config import settings


class DuckDBEngine:
    """DuckDB 执行引擎 - 支持持久化文件和 LanceDB"""

    def __init__(self, db_path: str = None, lancedb_dir: str = None):
        """
        初始化 DuckDB 引擎

        Args:
            db_path: DuckDB 数据库文件路径
            lancedb_dir: LanceDB 目录路径（可选）
        """
        # 默认使用持久化文件
        self.db_path = db_path or os.path.join(
            os.path.dirname(__file__), "..", "data", "duckdb.duckdb"
        )
        self.lancedb_dir = lancedb_dir
        self.conn: Optional[duckdb.DuckDBPyConnection] = None
        self._lock = threading.RLock()

    def connect(self):
        """建立连接（线程安全）"""
        with self._lock:
            # 如果已有连接，直接返回
            if self.conn is not None:
                try:
                    # 测试连接是否有效
                    self.conn.execute("SELECT 1")
                    return
                except Exception:
                    self.conn = None

            # 确保目录存在
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)

            # 尝试连接，使用 read_only 模式避免锁冲突
            try:
                self.conn = duckdb.connect(self.db_path, read_only=False)
            except Exception as e:
                # 如果失败，尝试只读模式
                try:
                    self.conn = duckdb.connect(self.db_path, read_only=True)
                except Exception:
                    # 如果都失败，创建一个新连接
                    self.conn = duckdb.connect(database=":memory:")

    def close(self):
        """关闭连接（线程安全）"""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None

    def execute(self, sql: str, params: List = None) -> Dict[str, Any]:
        """执行 SQL（线程安全）"""
        with self._lock:
            if not self.conn:
                self.connect()

            params = params or []

            sql = sql.strip().rstrip(";")
            if "LIMIT" not in sql.upper():
                sql = f"{sql} LIMIT {settings.SQL_DEFAULT_LIMIT}"

            start_time = time.time()
            try:
                if params:
                    result = self.conn.execute(sql, params)
                else:
                    result = self.conn.execute(sql)

                description = result.description
                if description:
                    columns = [desc[0] for desc in description]
                    raw_rows = result.fetchall()
                    row_count = len(raw_rows)
                    # 转换 Decimal 为普通类型
                    rows = []
                    for row in raw_rows:
                        converted_row = {}
                        for col, val in zip(columns, row):
                            if isinstance(val, Decimal):
                                converted_row[col] = float(val)
                            else:
                                converted_row[col] = val
                        rows.append(converted_row)
                else:
                    columns = []
                    rows = []
                    row_count = 0

                execution_time_ms = int((time.time() - start_time) * 1000)
                return {
                    "rows": rows,
                    "columns": columns,
                    "row_count": row_count,
                    "execution_time_ms": execution_time_ms
                }

            except Exception as e:
                return {
                    "rows": [],
                    "columns": [],
                    "row_count": 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000),
                    "error": str(e)
                }

    def execute_command(self, sql: str, params: List = None) -> None:
        """执行非查询 SQL（线程安全，不自动追加 LIMIT）"""
        with self._lock:
            if not self.conn:
                self.connect()
            if params:
                self.conn.execute(sql, params)
            else:
                self.conn.execute(sql)

    def explain(self, sql: str, params: List = None) -> Dict[str, Any]:
        """执行 EXPLAIN 预检（不真正执行查询）"""
        with self._lock:
            if not self.conn:
                self.connect()

            params = params or []
            sql = (sql or "").strip().rstrip(";")
            if not sql:
                return {"ok": False, "error": "SQL 语句不能为空"}

            try:
                if params:
                    rows = self.conn.execute(f"EXPLAIN {sql}", params).fetchall()
                else:
                    rows = self.conn.execute(f"EXPLAIN {sql}").fetchall()
                return {"ok": True, "plan": rows[:20]}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        with self._lock:
            if not self.conn:
                self.connect()
            try:
                self.conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1")
                return True
            except Exception:
                return False

    def get_tables(self) -> List[str]:
        """获取所有表和视图"""
        with self._lock:
            if not self.conn:
                self.connect()
            try:
                result = self.conn.execute("SHOW TABLES").fetchall()
                return [r[0] for r in result]
            except Exception:
                return []

    def get_views(self) -> List[str]:
        """获取所有视图"""
        with self._lock:
            if not self.conn:
                self.connect()
            try:
                result = self.conn.execute(
                    "SELECT table_name FROM information_schema.views"
                ).fetchall()
                return [r[0] for r in result]
            except Exception:
                return []

    def get_schema(self, table_name: str) -> List[Dict]:
        """获取表结构"""
        with self._lock:
            if not self.conn:
                self.connect()
            try:
                result = self.conn.execute(f"DESCRIBE {table_name}").fetchall()
                return [{"column_name": r[0], "column_type": r[1], "null": r[2]} for r in result]
            except Exception:
                return []

    def create_view(self, view_name: str, sql: str) -> bool:
        """
        创建视图

        Args:
            view_name: 视图名称
            sql: SELECT 语句

        Returns:
            是否成功
        """
        with self._lock:
            if not self.conn:
                self.connect()
            try:
                self.conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS {sql}")
                return True
            except Exception as e:
                print(f"[DuckDB] 创建视图失败: {e}")
                return False

    def read_csv(self, path: str, table_name: str = None) -> str:
        """读取 CSV 文件并创建表"""
        with self._lock:
            if not self.conn:
                self.connect()
            if not table_name:
                import uuid
                table_name = f"tmp_csv_{uuid.uuid4().hex[:8]}"
            try:
                self.conn.execute(
                    f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_csv_auto('{path}')"
                )
                return table_name
            except Exception as e:
                print(f"[DuckDB] 读取 CSV 失败: {e}")
                return None

    def read_parquet(self, path: str, table_name: str = None) -> str:
        """读取 Parquet 文件并创建表"""
        with self._lock:
            if not self.conn:
                self.connect()
            if not table_name:
                import uuid
                table_name = f"tmp_parquet_{uuid.uuid4().hex[:8]}"
            try:
                self.conn.execute(
                    f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM read_parquet('{path}')"
                )
                return table_name
            except Exception as e:
                print(f"[DuckDB] 读取 Parquet 失败: {e}")
                return None

    def load_from_lancedb(self, lancedb_dir: str = None) -> bool:
        """
        从 LanceDB 加载数据到 DuckDB

        Args:
            lancedb_dir: LanceDB 目录路径

        Returns:
            是否成功
        """
        lancedb_dir = lancedb_dir or self.lancedb_dir
        if not lancedb_dir:
            return False

        with self._lock:
            if not self.conn:
                self.connect()

            try:
                # 使用 LanceDB 连接读取数据
                self.conn.execute(f"""
                    CREATE OR REPLACE TABLE events AS
                    SELECT * FROM lance_scan('{lancedb_dir}')
                """)
                return True
            except Exception as e:
                print(f"[DuckDB] 从 LanceDB 加载失败: {e}")
                return False


# 全局单例
_engine: Optional[DuckDBEngine] = None
_engine_lock = threading.Lock()


def get_engine(db_path: str = None) -> DuckDBEngine:
    """获取全局 DuckDB 引擎（线程安全）"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = DuckDBEngine(db_path=db_path)
    return _engine
