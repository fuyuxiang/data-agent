"""
查询追踪与监控模块

功能：
1. 记录每次查询的完整链路（问题 → 意图识别 → SQL 生成 → 执行结果）
2. 记录错误信息和堆栈
3. 计算各环节耗时
4. 提供查询接口和统计分析
5. 支持持久化到数据库或文件
"""

import hashlib
import json
import re
import sqlite3
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from uuid import uuid4


def normalize_question(text: str) -> str:
    """将用户问题归一化，用于缓存匹配。

    规则：
    - 去除首尾空白
    - 统一为小写
    - 移除具体数字（"最近30天" → "最近N天"，"20条" → "N条"）
    - 移除多余空格
    """
    t = text.strip().lower()
    # "最近30天" → "最近N天"，"近7小时" → "近N小时"
    t = re.sub(r'(\d+)\s*(天|小时|月|周|年)', r'N\2', t)
    # "20条" → "N条"，"前10" → "前N"
    t = re.sub(r'(\d+)\s*(条|个|件|次|项)', r'N\2', t)
    t = re.sub(r'(前|top|TOP)\s*\d+', r'\1N', t, flags=re.IGNORECASE)
    # 精确日期 "2026-01-01" → "DATE"
    t = re.sub(r'\d{4}-\d{2}-\d{2}', 'DATE', t)
    # 压缩空格
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def question_hash(text: str) -> str:
    """对归一化后的问题计算 MD5 哈希"""
    normalized = normalize_question(text)
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def normalize_table_scope(table_scope: Optional[Union[str, Iterable[str]]]) -> str:
    """规范化表作用域，用于缓存隔离"""
    if table_scope is None:
        return ""
    if isinstance(table_scope, str):
        values = [table_scope]
    else:
        values = list(table_scope)
    normalized = sorted({str(v).strip().lower() for v in values if str(v).strip()})
    return ",".join(normalized)


def scoped_question_hash(text: str, table_scope: Optional[Union[str, Iterable[str]]] = None) -> str:
    """按问题+表作用域生成缓存 key，避免跨表域误命中"""
    normalized_q = normalize_question(text)
    scope = normalize_table_scope(table_scope)
    key = f"{normalized_q}||{scope}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


@dataclass
class TraceStep:
    """单个执行步骤的追踪信息"""
    step_name: str  # 步骤名称（如 "parse_question", "generate_sql", "execute_sql"）
    start_time: float  # 开始时间戳
    end_time: Optional[float] = None  # 结束时间戳
    duration_ms: Optional[float] = None  # 耗时（毫秒）
    status: str = "running"  # 状态：running, success, error
    input_data: Optional[Dict] = None  # 输入数据
    output_data: Optional[Dict] = None  # 输出数据
    error_message: Optional[str] = None  # 错误信息
    error_traceback: Optional[str] = None  # 错误堆栈

    def finish(self, status: str = "success", output_data: Optional[Dict] = None, error: Optional[Exception] = None):
        """标记步骤完成"""
        self.end_time = time.time()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        self.status = status
        if output_data:
            self.output_data = output_data
        if error:
            self.error_message = str(error)
            self.error_traceback = traceback.format_exc()


@dataclass
class QueryTrace:
    """完整查询的追踪信息"""
    trace_id: str = field(default_factory=lambda: str(uuid4()))  # 唯一追踪ID
    question: str = ""  # 用户问题
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())  # 查询时间
    user_id: Optional[str] = None  # 用户ID（可选）
    session_id: Optional[str] = None  # 会话ID（可选）

    # 执行步骤
    steps: List[TraceStep] = field(default_factory=list)

    # 最终结果
    intent: Optional[str] = None  # 查询意图
    sql: Optional[str] = None  # 生成的SQL
    sql_params: Optional[List] = None  # SQL参数
    result_count: Optional[int] = None  # 结果数量
    final_answer: Optional[Any] = None  # 最终答案

    # 状态与性能
    status: str = "running"  # 整体状态：running, success, error
    total_duration_ms: Optional[float] = None  # 总耗时
    error_message: Optional[str] = None  # 错误信息

    # 元数据
    metadata: Dict = field(default_factory=dict)  # 额外元数据

    def add_step(self, step_name: str, input_data: Optional[Dict] = None) -> TraceStep:
        """添加新步骤"""
        step = TraceStep(
            step_name=step_name,
            start_time=time.time(),
            input_data=input_data
        )
        self.steps.append(step)
        return step

    def finish(self, status: str = "success", error: Optional[Exception] = None):
        """标记查询完成"""
        self.status = status
        if error:
            self.error_message = str(error)

        # 计算总耗时
        if self.steps:
            first_step_start = self.steps[0].start_time
            last_step_end = max((s.end_time for s in self.steps if s.end_time), default=time.time())
            self.total_duration_ms = (last_step_end - first_step_start) * 1000

    def to_dict(self) -> Dict:
        """转换为字典（用于序列化）"""
        data = asdict(self)
        # 简化 steps 数据（移除过大的字段）
        if data.get("steps"):
            for step in data["steps"]:
                # 限制输入输出数据大小
                if step.get("input_data"):
                    step["input_data"] = self._truncate_data(step["input_data"])
                if step.get("output_data"):
                    step["output_data"] = self._truncate_data(step["output_data"])
        return data

    @staticmethod
    def _truncate_data(data: Any, max_length: int = 1000) -> Any:
        """截断过长的数据"""
        if isinstance(data, str) and len(data) > max_length:
            return data[:max_length] + "... (truncated)"
        if isinstance(data, dict):
            return {k: QueryTrace._truncate_data(v, max_length) for k, v in data.items()}
        if isinstance(data, list) and len(data) > 10:
            return data[:10] + ["... (truncated)"]
        return data

    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class TraceManager:
    """追踪管理器 - 负责持久化和查询"""

    def __init__(self, db_path: Optional[Path] = None, enable_file_log: bool = False, log_dir: Optional[Path] = None):
        """
        初始化追踪管理器

        Args:
            db_path: SQLite数据库路径（用于持久化）
            enable_file_log: 是否启用文件日志
            log_dir: 日志文件目录
        """
        self.db_path = db_path
        self.enable_file_log = enable_file_log
        self.log_dir = Path(log_dir) if log_dir else Path("logs/traces")

        if self.db_path:
            self._init_db()

        if self.enable_file_log:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_traces (
                trace_id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                question_hash TEXT,
                timestamp TEXT NOT NULL,
                user_id TEXT,
                session_id TEXT,
                intent TEXT,
                sql TEXT,
                sql_params TEXT,
                result_count INTEGER,
                status TEXT,
                total_duration_ms REAL,
                error_message TEXT,
                trace_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # SQL 缓存池
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sql_cache (
                question_hash TEXT PRIMARY KEY,
                question_sample TEXT NOT NULL,
                intent TEXT NOT NULL,
                sql TEXT NOT NULL,
                table_scope TEXT DEFAULT '',
                hit_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_hit_at TIMESTAMP
            )
        """)
        # 兼容历史版本：若旧表缺少 table_scope 列则补齐
        try:
            cols = conn.execute("PRAGMA table_info(sql_cache)").fetchall()
            col_names = {str(row[1]).lower() for row in cols}
            if "table_scope" not in col_names:
                conn.execute("ALTER TABLE sql_cache ADD COLUMN table_scope TEXT DEFAULT ''")
        except Exception:
            pass
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON query_traces(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status ON query_traces(status)
        """)
        conn.commit()
        conn.close()

    def save_trace(self, trace: QueryTrace):
        """保存追踪记录"""
        # 保存到数据库
        if self.db_path:
            self._save_to_db(trace)
            # 成功查询自动写入 SQL 缓存池
            if trace.status == "success" and trace.sql:
                self._save_to_cache(trace)

        # 保存到文件
        if self.enable_file_log:
            self._save_to_file(trace)

    def _save_to_db(self, trace: QueryTrace):
        """保存到数据库"""
        conn = sqlite3.connect(self.db_path)
        q_hash = question_hash(trace.question) if trace.question else None
        conn.execute("""
            INSERT OR REPLACE INTO query_traces
            (trace_id, question, question_hash, timestamp, user_id, session_id, intent, sql, sql_params,
             result_count, status, total_duration_ms, error_message, trace_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trace.trace_id,
            trace.question,
            q_hash,
            trace.timestamp,
            trace.user_id,
            trace.session_id,
            trace.intent,
            trace.sql,
            json.dumps(trace.sql_params) if trace.sql_params else None,
            trace.result_count,
            trace.status,
            trace.total_duration_ms,
            trace.error_message,
            trace.to_json()
        ))
        conn.commit()
        conn.close()

    def _save_to_file(self, trace: QueryTrace):
        """保存到文件"""
        date_str = datetime.now().strftime("%Y%m%d")
        log_file = self.log_dir / f"trace_{date_str}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(trace.to_json() + "\n")

    def _save_to_cache(self, trace: QueryTrace):
        """将成功查询写入 sql_cache 表"""
        table_scope = normalize_table_scope((trace.metadata or {}).get("selected_tables"))
        q_hash = scoped_question_hash(trace.question, table_scope)
        now = datetime.now().isoformat()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO sql_cache (question_hash, question_sample, intent, sql, table_scope, hit_count, created_at, last_hit_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, NULL)
                ON CONFLICT(question_hash) DO UPDATE SET
                    intent = excluded.intent,
                    sql = excluded.sql,
                    question_sample = excluded.question_sample,
                    table_scope = excluded.table_scope
            """, (q_hash, trace.question, trace.intent, trace.sql, table_scope, now))
            conn.commit()
            conn.close()
        except Exception:
            pass  # 缓存写入失败不影响主流程

    def lookup_sql_cache(self, q_text: str, table_scope: Optional[Union[str, Iterable[str]]] = None) -> Optional[Tuple[str, str]]:
        """根据问题查找 SQL 缓存。"""
        if not self.db_path:
            return None

        scoped_hash = scoped_question_hash(q_text, table_scope)
        legacy_hash = question_hash(q_text)
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT intent, sql, question_hash FROM sql_cache WHERE question_hash = ?",
                (scoped_hash,)
            ).fetchone()
            # 向后兼容：旧缓存只按 question_hash 存储
            if not row:
                row = conn.execute(
                    "SELECT intent, sql, question_hash FROM sql_cache WHERE question_hash = ?",
                    (legacy_hash,)
                ).fetchone()

            if row and row["sql"]:
                conn.execute(
                    "UPDATE sql_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE question_hash = ?",
                    (datetime.now().isoformat(), row["question_hash"])
                )
                conn.commit()
                conn.close()
                return (row["intent"], row["sql"])

            conn.close()
        except Exception:
            pass
        return None

    def get_trace(self, trace_id: str) -> Optional[QueryTrace]:
        """根据ID获取追踪记录"""
        if not self.db_path:
            return None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM query_traces WHERE trace_id = ?", (trace_id,)).fetchone()
        conn.close()

        if not row:
            return None

        trace_data = json.loads(row["trace_data"])
        return QueryTrace(**trace_data)

    def query_traces(
        self,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        """查询追踪记录"""
        if not self.db_path:
            return []

        where_clauses = []
        params = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if user_id:
            where_clauses.append("user_id = ?")
            params.append(user_id)
        if start_time:
            where_clauses.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            where_clauses.append("timestamp <= ?")
            params.append(end_time)

        where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM query_traces {where_sql} ORDER BY timestamp DESC LIMIT ?",
            params + [limit]
        ).fetchall()
        conn.close()

        return [dict(row) for row in rows]

    def get_statistics(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> Dict:
        """获取统计信息"""
        if not self.db_path:
            return {}

        where_clauses = []
        params = []

        if start_time:
            where_clauses.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            where_clauses.append("timestamp <= ?")
            params.append(end_time)

        where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        stats = {}

        # 总查询数
        stats["total_queries"] = conn.execute(f"SELECT COUNT(*) as cnt FROM query_traces {where_sql}", params).fetchone()["cnt"]

        # 成功/失败数
        success_sql = f"SELECT COUNT(*) as cnt FROM query_traces {where_sql}"
        if where_clauses:
            success_sql += " AND status = 'success'"
        else:
            success_sql += " WHERE status = 'success'"
        stats["success_count"] = conn.execute(success_sql, params).fetchone()["cnt"]

        # 平均耗时
        avg_sql = f"SELECT AVG(total_duration_ms) as avg FROM query_traces {where_sql}"
        if where_clauses:
            avg_sql += " AND total_duration_ms IS NOT NULL"
        else:
            avg_sql += " WHERE total_duration_ms IS NOT NULL"
        avg_duration = conn.execute(avg_sql, params).fetchone()["avg"]
        stats["avg_duration_ms"] = round(avg_duration, 2) if avg_duration else 0

        # 按意图分组统计
        intent_stats = conn.execute(f"SELECT intent, COUNT(*) as cnt FROM query_traces {where_sql} GROUP BY intent", params).fetchall()
        stats["by_intent"] = {row["intent"]: row["cnt"] for row in intent_stats}

        conn.close()
        return stats


# 全局单例
_trace_manager: Optional[TraceManager] = None


def init_trace_manager(db_path: Optional[Path] = None, enable_file_log: bool = True, log_dir: Optional[Path] = None):
    """初始化全局追踪管理器"""
    global _trace_manager
    _trace_manager = TraceManager(db_path=db_path, enable_file_log=enable_file_log, log_dir=log_dir)


def get_trace_manager() -> Optional[TraceManager]:
    """获取全局追踪管理器"""
    return _trace_manager
