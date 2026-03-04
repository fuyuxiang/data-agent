"""
安全护栏模块 - SQL 注入防护与安全检查

功能：
1. SQL 语句白名单检查（只允许 SELECT）
2. 禁止危险操作（DROP, DELETE, UPDATE, INSERT 等）
3. 防止注释注入（--, /*, #）
4. 防止堆叠查询（;）
5. 敏感表保护
6. 表名格式验证
7. LIMIT 自动限制
"""

import re
from typing import List, Optional


class SQLSecurityError(Exception):
    """SQL 安全检查失败异常"""
    pass


class SQLGuardrail:
    """SQL 安全护栏"""

    # 危险关键词黑名单（大小写不敏感）
    DANGEROUS_KEYWORDS = [
        "DROP",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "REPLACE",
        "EXEC",
        "EXECUTE",
        "PRAGMA",
        "ATTACH",
        "DETACH",
        "GRANT",
        "REVOKE",
    ]

    # 危险模式（正则表达式）
    DANGEROUS_PATTERNS = [
        r"--",  # SQL 注释
        r"/\*",  # 多行注释开始
        r"\*/",  # 多行注释结束
        r"#",  # MySQL 注释
        r";\s*\w",  # 堆叠查询（分号后跟语句）
        r"xp_",  # SQL Server 扩展存储过程
        r"sp_",  # SQL Server 系统存储过程
    ]

    # 敏感表名（根据实际业务调整）
    SENSITIVE_TABLES = [
        "users",
        "passwords",
        "credentials",
        "secrets",
        "admin",
        "user",
    ]

    # 允许访问的系统只读表（元数据查询）
    SYSTEM_READONLY_TABLES = [
        "information_schema",
    ]

    @classmethod
    def validate_sql(cls, sql: str, allow_multiple_statements: bool = False, allowed_tables: Optional[List[str]] = None) -> None:
        """
        验证 SQL 语句的安全性

        Args:
            sql: 待验证的 SQL 语句
            allow_multiple_statements: 是否允许多条语句（默认不允许）
            allowed_tables: 允许访问的表名白名单（可选）

        Raises:
            SQLSecurityError: 如果 SQL 不安全
        """
        if not sql or not sql.strip():
            raise SQLSecurityError("SQL 语句不能为空")

        sql_upper = sql.upper()

        # 1. 检查是否只包含 SELECT 语句
        if not sql_upper.strip().startswith("SELECT"):
            raise SQLSecurityError("只允许执行 SELECT 查询，禁止其他操作")

        # 2. 检查危险关键词
        for keyword in cls.DANGEROUS_KEYWORDS:
            if keyword in sql_upper:
                raise SQLSecurityError(f"检测到危险关键词: {keyword}")

        # 3. 检查危险模式
        for pattern in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, sql, re.IGNORECASE):
                raise SQLSecurityError(f"检测到危险模式: {pattern}")

        # 4. 检查堆叠查询（多条语句）
        if not allow_multiple_statements:
            # 简单检查：分号后面不应该有非空白字符
            if re.search(r";\s*\S", sql):
                raise SQLSecurityError("不允许执行多条 SQL 语句（堆叠查询）")

        # 5. 检查敏感表访问（可选）
        for table in cls.SENSITIVE_TABLES:
            # 使用单词边界匹配，避免误判（如 users_log 不应该被拦截）
            if re.search(rf"\b{table}\b", sql, re.IGNORECASE):
                raise SQLSecurityError(f"禁止访问敏感表: {table}")

        # 6. 检查表名白名单
        if allowed_tables:
            # 提取 SQL 中使用的表名
            table_pattern = r"(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
            used_tables = re.findall(table_pattern, sql, re.IGNORECASE)
            allowed_lower = [t.lower() for t in allowed_tables]
            system_lower = [t.lower() for t in cls.SYSTEM_READONLY_TABLES]
            for table in used_tables:
                lower_table = table.lower()
                if lower_table in system_lower:
                    continue
                if lower_table not in allowed_lower:
                    raise SQLSecurityError(f"表 {table} 不在允许访问的白名单中")

    @classmethod
    def sanitize_params(cls, params: List) -> List:
        """
        清理 SQL 参数（防止参数注入）

        Args:
            params: SQL 参数列表

        Returns:
            清理后的参数列表
        """
        sanitized = []
        for param in params:
            if isinstance(param, str):
                # 移除潜在的危险字符
                # 注意：这里只是额外保护，主要还是依赖参数化查询
                param = param.replace("--", "").replace("/*", "").replace("*/", "")
            sanitized.append(param)
        return sanitized

    @classmethod
    def validate_table_name(cls, table_name: str, allowed_tables: Optional[List[str]] = None) -> None:
        """
        验证表名是否合法

        Args:
            table_name: 表名
            allowed_tables: 允许访问的表名白名单（可选）

        Raises:
            SQLSecurityError: 如果表名不合法
        """
        # 检查表名格式（只允许字母、数字、下划线）
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
            raise SQLSecurityError(f"非法的表名格式: {table_name}")

        # 检查白名单
        if allowed_tables and table_name not in allowed_tables:
            raise SQLSecurityError(f"表 {table_name} 不在允许访问的白名单中")

        # 检查敏感表
        if table_name.lower() in [t.lower() for t in cls.SENSITIVE_TABLES]:
            raise SQLSecurityError(f"禁止访问敏感表: {table_name}")

    @classmethod
    def apply_limit(cls, sql: str, max_limit: int = 1000, default_limit: int = 20) -> str:
        """
        自动应用 LIMIT 限制

        Args:
            sql: SQL 语句
            max_limit: 最大限制
            default_limit: 默认限制

        Returns:
            添加了 LIMIT 的 SQL 语句
        """
        sql_upper = sql.upper()
        if "LIMIT" in sql_upper:
            # 已有 LIMIT，检查是否超过最大值
            match = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
            if match:
                current_limit = int(match.group(1))
                if current_limit > max_limit:
                    sql = re.sub(r"LIMIT\s+\d+", f"LIMIT {max_limit}", sql, flags=re.IGNORECASE)
        else:
            # 添加默认 LIMIT
            sql = f"{sql.strip().rstrip(';')} LIMIT {default_limit}"
        return sql
