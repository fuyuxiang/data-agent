"""
为现有 SQLite 库补齐媒体数据集相关 schema。

仓库当前没有完整 Alembic 环境，这里提供可重复执行的增量迁移脚本。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import Engine, inspect, text


DATASET_COLUMNS = {
    "processing_status": "TEXT DEFAULT 'ready'",
    "progress": "FLOAT DEFAULT 100",
    "error_message": "TEXT",
    "media_count": "INTEGER DEFAULT 0",
    "processed_count": "INTEGER DEFAULT 0",
    "failed_count": "INTEGER DEFAULT 0",
    "last_processed_at": "DATETIME",
}


def _ensure_columns(engine: Engine, table_name: str, columns: dict[str, str]) -> None:
    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def _execute_batch(engine: Engine, statements: Iterable[str]) -> None:
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def migrate_media_schema(engine: Optional[Engine] = None) -> None:
    if engine is None:
        from app.core.database import engine as default_engine

        engine = default_engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "datasets" in tables:
        _ensure_columns(engine, "datasets", DATASET_COLUMNS)

    _execute_batch(
        engine,
        [
            """
            CREATE TABLE IF NOT EXISTS dataset_media_resources (
                id INTEGER PRIMARY KEY,
                dataset_id INTEGER NOT NULL,
                resource_type TEXT NOT NULL,
                source_type TEXT NOT NULL,
                original_path VARCHAR(1024) NOT NULL,
                stored_path VARCHAR(1024),
                file_name VARCHAR(255),
                mime_type VARCHAR(255),
                file_size INTEGER,
                checksum VARCHAR(128),
                dedupe_key VARCHAR(255),
                status TEXT DEFAULT 'pending',
                media_metadata JSON,
                error_message TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                last_processed_at DATETIME,
                FOREIGN KEY(dataset_id) REFERENCES datasets (id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS image_indexes (
                id INTEGER PRIMARY KEY,
                dataset_id INTEGER NOT NULL,
                resource_id INTEGER NOT NULL UNIQUE,
                preview_path VARCHAR(1024),
                embedding JSON,
                ocr_text TEXT,
                caption_text TEXT,
                tags JSON,
                index_metadata JSON,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(dataset_id) REFERENCES datasets (id),
                FOREIGN KEY(resource_id) REFERENCES dataset_media_resources (id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS video_segment_indexes (
                id INTEGER PRIMARY KEY,
                dataset_id INTEGER NOT NULL,
                resource_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                segment_index INTEGER NOT NULL,
                start_sec FLOAT NOT NULL,
                end_sec FLOAT NOT NULL,
                keyframe_path VARCHAR(1024),
                embedding JSON,
                caption_text TEXT,
                asr_text TEXT,
                ocr_text TEXT,
                scene_tags JSON,
                object_tags JSON,
                index_metadata JSON,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(dataset_id) REFERENCES datasets (id),
                FOREIGN KEY(resource_id) REFERENCES dataset_media_resources (id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_dataset_media_resources_dataset_status ON dataset_media_resources (dataset_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_dataset_media_resources_dataset_dedupe ON dataset_media_resources (dataset_id, dedupe_key)",
            "CREATE INDEX IF NOT EXISTS ix_image_indexes_dataset_resource ON image_indexes (dataset_id, resource_id)",
            "CREATE INDEX IF NOT EXISTS ix_video_segment_indexes_dataset_video ON video_segment_indexes (dataset_id, video_id)",
            "CREATE INDEX IF NOT EXISTS ix_video_segment_indexes_resource_segment ON video_segment_indexes (resource_id, segment_index)",
        ],
    )


if __name__ == "__main__":
    migrate_media_schema()
    print("media schema migration completed")
