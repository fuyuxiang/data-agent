"""
初始化数据库表，并补齐媒体相关增量 schema。
"""
from app.core.database import Base, engine
from app.models.models import (
    AuditLog,
    CSVFile,
    DataSource,
    DataSourceSchema,
    Dataset,
    DatasetMediaResource,
    ImageIndex,
    QueryHistory,
    User,
    UserWorkspace,
    VideoSegmentIndex,
    Workspace,
)
from scripts.migrate_media_schema import migrate_media_schema


def init_db() -> None:
    Base.metadata.create_all(engine)
    migrate_media_schema(engine)
    print("数据库表创建/迁移成功!")


if __name__ == "__main__":
    init_db()
