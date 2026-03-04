"""
数据源服务 - 连接管理、Schema 探查
"""
import os
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, text
from cryptography.fernet import Fernet

from app.core.logging import get_logger
from app.core.config import settings
from app.models.models import DataSource, CSVFile, DataSourceSchema, DataSourceType

logger = get_logger(__name__)

# 加密密钥（从配置读取，如果未配置则生成一个警告）
def _get_fernet() -> Fernet:
    if settings.ENCRYPTION_KEY:
        return Fernet(settings.ENCRYPTION_KEY)
    else:
        # 生成临时密钥，仅用于开发环境
        logger.warning("未配置 ENCRYPTION_KEY，使用临时密钥。生产环境应配置永久密钥。")
        return Fernet(Fernet.generate_key())

fernet = _get_fernet()


def encrypt_password(password: str) -> str:
    """加密密码"""
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    """解密密码"""
    return fernet.decrypt(encrypted.encode()).decode()


class DataSourceService:
    """数据源服务"""

    def __init__(self, db: AsyncSession = None):
        self.db = db

    async def create_data_source(
        self,
        workspace_id: int,
        name: str,
        ds_type: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        connection_string: Optional[str] = None,
    ) -> DataSource:
        """创建数据源"""
        # 加密密码
        password_encrypted = None
        if password:
            password_encrypted = encrypt_password(password)

        data_source = DataSource(
            workspace_id=workspace_id,
            name=name,
            type=DataSourceType(ds_type),
            host=host,
            port=port,
            database=database,
            username=username,
            password_encrypted=password_encrypted,
            connection_string=connection_string,
        )

        self.db.add(data_source)
        await self.db.commit()
        await self.db.refresh(data_source)

        logger.info(f"创建数据源: {name} (ID: {data_source.id})")

        return data_source

    async def test_connection(
        self,
        ds_type: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        connection_string: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[int]]:
        """测试数据库连接"""
        try:
            if ds_type == "csv":
                # CSV 文件直接验证路径
                if connection_string and os.path.exists(connection_string):
                    return True, "CSV 文件连接成功", 1
                return False, "CSV 文件不存在", None

            # 其他数据库类型
            if ds_type == "mysql":
                import pymysql
                conn = pymysql.connect(
                    host=host,
                    port=port or 3306,
                    user=username,
                    password=password,
                    database=database,
                    connect_timeout=5,
                )
                cursor = conn.cursor()
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
                cursor.close()
                conn.close()
                return True, "连接成功", len(tables)

            elif ds_type == "postgresql":
                import psycopg2
                conn = psycopg2.connect(
                    host=host,
                    port=port or 5432,
                    user=username,
                    password=password,
                    database=database,
                    connect_timeout=5,
                )
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
                table_count = cursor.fetchone()[0]
                cursor.close()
                conn.close()
                return True, "连接成功", table_count

            elif ds_type == "sqlserver":
                import pymssql
                conn = pymssql.connect(
                    server=host,
                    port=port or 1433,
                    user=username,
                    password=password,
                    database=database,
                )
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM information_schema.tables")
                table_count = cursor.fetchone()[0]
                cursor.close()
                conn.close()
                return True, "连接成功", table_count

            else:
                return False, f"不支持的数据源类型: {ds_type}", None

        except Exception as e:
            logger.error(f"连接测试失败: {e}")
            return False, str(e), None

    async def refresh_schema(self, data_source: DataSource) -> Tuple[int, int]:
        """刷新数据源 Schema"""
        table_count = 0
        column_count = 0

        try:
            # 获取密码
            password = None
            if data_source.password_encrypted:
                password = decrypt_password(data_source.password_encrypted)

            # 根据类型连接
            if data_source.type == DataSourceType.CSV:
                # CSV 文件：遍历所有关联的 CSVFile
                # 先删除旧 Schema
                await self.db.execute(
                    delete(DataSourceSchema).where(DataSourceSchema.data_source_id == data_source.id)
                )

                # 获取所有 CSV 文件
                from sqlalchemy import select
                result = await self.db.execute(
                    select(CSVFile).where(CSVFile.data_source_id == data_source.id)
                )
                csv_files = result.scalars().all()

                if not csv_files:
                    logger.warning(f"数据源 {data_source.id} 没有关联的 CSV 文件")

                import pandas as pd
                for csv_file in csv_files:
                    if os.path.exists(csv_file.file_path):
                        try:
                            df = pd.read_csv(csv_file.file_path)
                            table_name = os.path.splitext(csv_file.filename)[0]

                            # 添加每个文件的 Schema
                            for col in df.columns:
                                schema = DataSourceSchema(
                                    data_source_id=data_source.id,
                                    table_name=table_name,
                                    column_name=col,
                                    column_type=str(df[col].dtype),
                                    is_nullable=True,
                                )
                                self.db.add(schema)
                                column_count += 1

                            table_count += 1
                        except Exception as e:
                            logger.error(f"读取 CSV 文件 {csv_file.filename} 失败: {e}")
                    else:
                        logger.warning(f"CSV 文件不存在: {csv_file.file_path}")

            elif data_source.type == DataSourceType.MYSQL:
                import pymysql
                conn = pymysql.connect(
                    host=data_source.host,
                    port=data_source.port or 3306,
                    user=data_source.username,
                    password=password,
                    database=data_source.database,
                )
                cursor = conn.cursor()

                # 获取表列表
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()

                for (table_name,) in tables:
                    # 获取列信息（使用 text 包装，但 table_name 来自数据库返回，可信）
                    cursor.execute(text(f"DESCRIBE `{table_name}`"))
                    columns = cursor.fetchall()

                    for col in columns:
                        column_name, col_type, nullable, key, default, extra = col
                        schema = DataSourceSchema(
                            data_source_id=data_source.id,
                            table_name=table_name,
                            column_name=column_name,
                            column_type=col_type,
                            is_primary_key=key == "PRI",
                            is_foreign_key=key == "MUL",
                            is_nullable=nullable == "YES",
                        )
                        self.db.add(schema)
                        column_count += 1

                    table_count += 1

                cursor.close()
                conn.close()

            elif data_source.type == DataSourceType.POSTGRESQL:
                import psycopg2
                conn = psycopg2.connect(
                    host=data_source.host,
                    port=data_source.port or 5432,
                    user=data_source.username,
                    password=password,
                    database=data_source.database,
                )
                cursor = conn.cursor()

                # 获取表列表
                cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                tables = cursor.fetchall()

                for (table_name,) in tables:
                    # 获取列信息
                    cursor.execute(text(f"""
                        SELECT column_name, data_type, is_nullable, column_default
                        FROM information_schema.columns
                        WHERE table_name = '{table_name}' AND table_schema = 'public'
                    """))
                    columns = cursor.fetchall()

                    for col in columns:
                        column_name, col_type, nullable, default = col
                        schema = DataSourceSchema(
                            data_source_id=data_source.id,
                            table_name=table_name,
                            column_name=column_name,
                            column_type=col_type,
                            is_nullable=nullable == "YES",
                        )
                        self.db.add(schema)
                        column_count += 1

                    table_count += 1

                cursor.close()
                conn.close()

            elif data_source.type == DataSourceType.SQLSERVER:
                import pymssql
                conn = pymssql.connect(
                    server=data_source.host,
                    port=data_source.port or 1433,
                    user=data_source.username,
                    password=password,
                    database=data_source.database,
                )
                cursor = conn.cursor()

                # 获取表列表
                cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
                tables = cursor.fetchall()

                for (table_name,) in tables:
                    # 获取列信息
                    cursor.execute(text(f"""
                        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_NAME = '{table_name}'
                    """))
                    columns = cursor.fetchall()

                    for col in columns:
                        column_name, col_type, nullable = col
                        schema = DataSourceSchema(
                            data_source_id=data_source.id,
                            table_name=table_name,
                            column_name=column_name,
                            column_type=col_type,
                            is_nullable=nullable == "YES",
                        )
                        self.db.add(schema)
                        column_count += 1

                    table_count += 1

                cursor.close()
                conn.close()

            elif data_source.type == DataSourceType.DUCKDB:
                import duckdb
                conn = duckdb.connect(data_source.connection_string or data_source.database or ':memory:')
                cursor = conn.cursor()

                # 获取表列表
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()

                for (table_name,) in tables:
                    # 获取列信息
                    cursor.execute(text(f"DESCRIBE {table_name}"))
                    columns = cursor.fetchall()

                    for col in columns:
                        column_name, col_type, nullable, default, primary_key, unique = col[:6]
                        schema = DataSourceSchema(
                            data_source_id=data_source.id,
                            table_name=table_name,
                            column_name=column_name,
                            column_type=col_type,
                            is_nullable=nullable == "YES",
                            is_primary_key=primary_key == True,
                        )
                        self.db.add(schema)
                        column_count += 1

                    table_count += 1

                conn.close()

            else:
                raise NotImplementedError(f"不支持的数据源类型: {data_source.type}")

            await self.db.commit()
            logger.info(f"Schema 刷新完成: {table_count} 表, {column_count} 列")

        except NotImplementedError:
            # 对于暂不支持的类型，直接抛出
            raise
        except Exception as e:
            logger.error(f"Schema 刷新失败: {e}")
            await self.db.rollback()
            raise

        return table_count, column_count
