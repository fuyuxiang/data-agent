"""
初始化数据库表（同步版本）
"""
from app.core.database import engine, Base
from app.models.models import (
    Workspace, User, UserWorkspace, DataSource, CSVFile, DataSourceSchema,
    Dataset, Conversation, Message, QueryHistory, AuditLog
)

def init_db():
    # 创建所有表
    Base.metadata.create_all(engine)
    print("数据库表创建成功!")

if __name__ == "__main__":
    init_db()
