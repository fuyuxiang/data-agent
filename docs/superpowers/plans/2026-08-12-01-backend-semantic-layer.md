# 后端基础与语义层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭起后端骨架，实现语义模型（数据集/字段/枚举/指标）的存储、加载与语义体检，为 SQL 编译器提供确定性输入。

**Architecture:** FastAPI + SQLAlchemy 2.0 同步 ORM。元数据存 PostgreSQL 的 `agent_meta` schema，样本业务数据存 `sample` schema，两者物理隔离便于验证列权限。语义模型从数据库加载为不可变的内存对象树（`SemanticModel`），编译器只读它，不碰 ORM。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.0、PostgreSQL 15+、Pydantic v2、pytest、psycopg2-binary

## Global Constraints

以下约束来自 `docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md`，每个任务的要求都隐含包含本节：

- LLM 在整条链路中只有一个介入点（意图识别），**不生成 SQL**。
- 「允许聚合」是编译器硬约束，不是提示：字段未标 Sum，编译器不产出 SUM。
- 语义体检（Semantic Lint）不通过的数据集，不允许被问答使用。
- 无权限的问题必须拒答**且不泄漏元数据**（不得回显表名、字段名是否存在）。
- 安全相关测试必须 100% 通过，不接受已知失败。
- SQL 方言本轮只支持 PostgreSQL。
- 本轮限定单数据集，不做多表 Join。
- 比率指标标注 `Recalculate`，禁止对比率求和。
- 指标必须显式声明时间口径（按哪个日期字段计算）。
- 代码注释与标识符用英文；文档与提交信息用中文。

---

### Task 1: 项目骨架与配置

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/db.py`
- Create: `backend/app/main.py`
- Create: `backend/pytest.ini`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_health.py`
- Create: `backend/.env.example`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `app.core.config.Settings` — 含 `meta_database_url: str`、`sample_database_url: str`、`llm_api_key: str`、`llm_base_url: str`、`llm_model: str`、`clarify_confidence_threshold: float`、`clarify_max_rounds: int`、`max_result_rows: int`、`query_timeout_seconds: int`
  - `app.core.config.get_settings() -> Settings`（`lru_cache` 单例）
  - `app.core.db.MetaSession` — `sessionmaker`，元数据库会话
  - `app.core.db.get_meta_session()` — FastAPI 依赖，yield `Session`
  - `app.main.app` — FastAPI 实例

- [ ] **Step 1: 写依赖清单**

`backend/requirements.txt`：

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
sqlalchemy>=2.0.35
psycopg2-binary>=2.9.9
pydantic>=2.9.0
pydantic-settings>=2.6.0
python-jose[cryptography]>=3.3.0
passlib[bcrypt]>=1.7.4
sqlglot>=25.24.0
openai>=1.54.0
pytest>=8.3.0
pytest-asyncio>=0.24.0
httpx>=0.27.0
```

不引入 pandas / numpy / langchain：本轮无需要它们的场景，编译器与执行器只处理行列表。

- [ ] **Step 2: 写失败的健康检查测试**

`backend/tests/test_health.py`：

```python
from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_health.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 4: 写配置模块**

`backend/app/core/config.py`：

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    meta_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"
    sample_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"

    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # Clarification thresholds are global for this iteration (spec 5.2).
    clarify_confidence_threshold: float = 0.7
    clarify_max_rounds: int = 2

    # Cost guardrails (spec M-13).
    max_result_rows: int = 1000
    query_timeout_seconds: int = 30
    cost_warn_rows: int = 1_000_000
    cost_reject_rows: int = 50_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 写数据库会话模块**

`backend/app/core/db.py`：

```python
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

meta_engine = create_engine(_settings.meta_database_url, pool_pre_ping=True)
MetaSession = sessionmaker(bind=meta_engine, expire_on_commit=False)

# Separate engine for business data: keeps metadata credentials distinct from
# the credentials used to run generated SQL.
sample_engine = create_engine(
    _settings.sample_database_url,
    pool_pre_ping=True,
    connect_args={"options": f"-c statement_timeout={_settings.query_timeout_seconds * 1000}"},
)


def get_meta_session() -> Iterator[Session]:
    session = MetaSession()
    try:
        yield session
    finally:
        session.close()
```

- [ ] **Step 6: 写应用入口**

`backend/app/main.py`：

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Data Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

`backend/app/__init__.py` 留空文件。`backend/tests/__init__.py` 留空文件。

- [ ] **Step 7: 写 pytest 配置**

`backend/pytest.ini`：

```ini
[pytest]
pythonpath = .
testpaths = tests
addopts = -ra
```

- [ ] **Step 8: 写环境变量样例**

`backend/.env.example`：

```
META_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent
SAMPLE_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent
JWT_SECRET=change-me
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
```

- [ ] **Step 9: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 10: 提交**

```bash
git add backend/requirements.txt backend/pytest.ini backend/.env.example backend/app backend/tests
git commit -F - <<'EOF'
搭建后端项目骨架与配置层

新一轮实现从空目录开始，需要先确立配置、数据库会话与测试入口，后续语义层与编译器才有落点。元数据库与业务库使用独立 engine，避免执行生成 SQL 时复用元数据凭据。

- 引入 FastAPI 骨架与健康检查接口
- 配置层集中管理澄清阈值、成本护栏与 LLM 参数
- 业务库连接注入 statement_timeout，为成本护栏留出底线
- 验证：pytest tests/test_health.py 通过
EOF
```

---

### Task 2: 语义层枚举与 ORM 模型

**Files:**
- Create: `backend/app/semantic/__init__.py`
- Create: `backend/app/semantic/enums.py`
- Create: `backend/app/semantic/orm.py`
- Create: `backend/tests/semantic/__init__.py`
- Create: `backend/tests/semantic/test_orm_schema.py`

**Interfaces:**
- Consumes: `app.core.db.meta_engine`、`app.core.db.MetaSession`（Task 1）
- Produces:
  - `app.semantic.enums.SemanticType` — `AMOUNT/QUANTITY/RATIO/DATE/ID/ENUM/TEXT`
  - `app.semantic.enums.Aggregation` — `SUM/COUNT/DISTINCT_COUNT/AVG/MAX/MIN/NONE`
  - `app.semantic.enums.MetricKind` — `ATOMIC/DERIVED/COMPOSITE/RATIO`
  - `app.semantic.enums.AggregationBehavior` — `ADDITIVE/RECALCULATE/LAST_VALUE`
  - `app.semantic.enums.Sensitivity` — `PUBLIC/INTERNAL/SENSITIVE`
  - `app.semantic.orm.Base` — declarative base
  - ORM 类 `DatasetRow`、`FieldRow`、`EnumValueRow`、`MetricRow`，表名分别为 `agent_meta.dataset`、`agent_meta.field`、`agent_meta.enum_value`、`agent_meta.metric`

- [ ] **Step 1: 写失败的模型结构测试**

`backend/tests/semantic/test_orm_schema.py`：

```python
from app.semantic.enums import Aggregation, MetricKind, SemanticType
from app.semantic.orm import DatasetRow, FieldRow, MetricRow


def test_dataset_has_forbidden_scenario_column():
    # Spec M-01: forbidden_scenario is the only mechanism that stops the agent
    # from computing finance-confirmed revenue off an orders table.
    assert "forbidden_scenario" in DatasetRow.__table__.columns


def test_field_carries_allowed_aggregations():
    assert "allowed_aggregations" in FieldRow.__table__.columns
    assert "default_aggregation" in FieldRow.__table__.columns


def test_metric_requires_time_field():
    # Spec 4.4: a metric must declare which date column it is measured on.
    assert MetricRow.__table__.columns["time_field"].nullable is False


def test_enums_cover_spec_values():
    assert SemanticType.AMOUNT.value == "amount"
    assert Aggregation.DISTINCT_COUNT.value == "distinct_count"
    assert MetricKind.RATIO.value == "ratio"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/semantic/test_orm_schema.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.semantic'`

- [ ] **Step 3: 写枚举模块**

`backend/app/semantic/enums.py`：

```python
from enum import Enum


class SemanticType(str, Enum):
    AMOUNT = "amount"
    QUANTITY = "quantity"
    RATIO = "ratio"
    DATE = "date"
    ID = "id"
    ENUM = "enum"
    TEXT = "text"


class Aggregation(str, Enum):
    SUM = "sum"
    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    NONE = "none"


class MetricKind(str, Enum):
    ATOMIC = "atomic"
    DERIVED = "derived"
    COMPOSITE = "composite"
    RATIO = "ratio"


class AggregationBehavior(str, Enum):
    """How a metric behaves when rolled up across rows."""

    ADDITIVE = "additive"
    RECALCULATE = "recalculate"
    LAST_VALUE = "last_value"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
```

- [ ] **Step 4: 写 ORM 模型**

`backend/app/semantic/orm.py`：

```python
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

META_SCHEMA = "agent_meta"


class Base(DeclarativeBase):
    metadata = MetaData(schema=META_SCHEMA)


class DatasetRow(Base):
    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    business_name: Mapped[str] = mapped_column(String(128))
    physical_table: Mapped[str] = mapped_column(String(256))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    grain: Mapped[str] = mapped_column(String(128), default="")
    applicable_scenario: Mapped[str] = mapped_column(Text, default="")
    forbidden_scenario: Mapped[str] = mapped_column(Text, default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    fields: Mapped[list["FieldRow"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )
    metrics: Mapped[list["MetricRow"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class FieldRow(Base):
    __tablename__ = "field"
    __table_args__ = (UniqueConstraint("dataset_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    physical_column: Mapped[str] = mapped_column(String(128))
    business_name: Mapped[str] = mapped_column(String(128), default="")
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    semantic_type: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(32), default="")
    display_format: Mapped[str] = mapped_column(String(32), default="")
    default_aggregation: Mapped[str] = mapped_column(String(16), default="none")
    allowed_aggregations: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_groupable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_queryable: Mapped[bool] = mapped_column(Boolean, default=True)
    sensitivity: Mapped[str] = mapped_column(String(16), default="public")

    # Reserved for the next iteration (spec 4.2): stored but never read by the
    # compiler and not exposed in the config UI.
    business_object: Mapped[str] = mapped_column(String(128), default="")
    pii_level: Mapped[str] = mapped_column(String(16), default="")
    default_recall: Mapped[bool] = mapped_column(Boolean, default=False)
    value_recall: Mapped[bool] = mapped_column(Boolean, default=False)

    dataset: Mapped[DatasetRow] = relationship(back_populates="fields")
    enum_values: Mapped[list["EnumValueRow"]] = relationship(
        back_populates="field", cascade="all, delete-orphan"
    )


class EnumValueRow(Base):
    __tablename__ = "enum_value"
    __table_args__ = (UniqueConstraint("field_id", "physical_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("field.id", ondelete="CASCADE"))
    physical_value: Mapped[str] = mapped_column(String(128))
    business_value: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    description: Mapped[str] = mapped_column(Text, default="")

    field: Mapped[FieldRow] = relationship(back_populates="enum_values")


class MetricRow(Base):
    __tablename__ = "metric"
    __table_args__ = (UniqueConstraint("dataset_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    business_name: Mapped[str] = mapped_column(String(128))
    synonyms: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16))
    aggregation_behavior: Mapped[str] = mapped_column(String(16), default="additive")

    # ATOMIC: aggregation over source_field. DERIVED: same plus fixed_filter.
    # COMPOSITE / RATIO: expression referencing other metric names.
    source_field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fixed_filter: Mapped[str] = mapped_column(Text, default="")
    expression: Mapped[str] = mapped_column(Text, default="")

    # Spec 4.4: the date column this metric is measured on. Not nullable —
    # an unstated time basis is the usual cause of one metric yielding two numbers.
    time_field: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), default="")
    display_format: Mapped[str] = mapped_column(String(32), default="")
    owner: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    dataset: Mapped[DatasetRow] = relationship(back_populates="metrics")
```

`backend/app/semantic/__init__.py` 与 `backend/tests/semantic/__init__.py` 留空。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/semantic/test_orm_schema.py -v`
Expected: PASS（4 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/semantic backend/tests/semantic
git commit -F - <<'EOF'
定义语义层枚举与元数据 ORM 模型

编译器需要一份确定性的语义输入，因此先固化数据集、字段、枚举值、指标四张元数据表的结构。字段表按设计文档只让本轮生效的属性参与编译，其余属性建列但不读取，避免下一轮改表。

- 数据集表保留禁用场景字段，作为阻止跨口径取数的机制
- 字段表以数组列存允许聚合，供编译器做硬约束校验
- 指标表的时间口径列设为非空，杜绝口径缺失
- 元数据统一落在 agent_meta schema，与业务数据物理隔离
- 验证：pytest tests/semantic/test_orm_schema.py 4 项通过
EOF
```

---

### Task 3: 样本业务数据与建表脚本

**Files:**
- Create: `backend/scripts/__init__.py`
- Create: `backend/scripts/init_db.py`
- Create: `backend/scripts/sample_data.sql`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_sample_data.py`

**Interfaces:**
- Consumes: `app.semantic.orm.Base`、`app.core.db.meta_engine`、`app.core.db.sample_engine`（Task 1、2）
- Produces:
  - `scripts.init_db.create_schemas()` — 创建 `agent_meta` 与 `sample` schema
  - `scripts.init_db.create_meta_tables()` — 建元数据表
  - `scripts.init_db.load_sample_data()` — 执行 `sample_data.sql`
  - `scripts.init_db.main()` — 依次执行上述三步
  - 测试 fixture `meta_session`（函数级，自动回滚）、`sample_conn`
  - 样本表 `sample.orders`，列：`order_id`、`order_no`、`customer_id`、`customer_name`、`region_code`、`province`、`channel`、`amount`、`cost`、`quantity`、`is_new_customer`、`status`、`created_date`、`completed_date`

- [ ] **Step 1: 写样本数据 SQL**

`backend/scripts/sample_data.sql`。数据要能验证：区域筛选、省份分组、时间对比（跨月）、新客派生指标、毛利率复合指标、渠道枚举映射。

```sql
DROP TABLE IF EXISTS sample.orders;

CREATE TABLE sample.orders (
    order_id        SERIAL PRIMARY KEY,
    order_no        VARCHAR(32)  NOT NULL,
    customer_id     INTEGER      NOT NULL,
    customer_name   VARCHAR(64)  NOT NULL,
    region_code     VARCHAR(8)   NOT NULL,
    province        VARCHAR(32)  NOT NULL,
    channel         VARCHAR(16)  NOT NULL,
    amount          NUMERIC(14,2) NOT NULL,
    cost            NUMERIC(14,2) NOT NULL,
    quantity        INTEGER      NOT NULL,
    is_new_customer BOOLEAN      NOT NULL,
    status          VARCHAR(16)  NOT NULL,
    created_date    DATE         NOT NULL,
    completed_date  DATE
);

-- July 2026 (comparison baseline) and August 2026 (current period).
-- EC = 华东, SC = 华南, NC = 华北.
INSERT INTO sample.orders
    (order_no, customer_id, customer_name, region_code, province, channel,
     amount, cost, quantity, is_new_customer, status, created_date, completed_date)
VALUES
    ('SO202607001', 1001, '江苏机械', 'EC', '江苏', 'online',  120000.00,  78000.00, 12, false, 'completed', '2026-07-03', '2026-07-05'),
    ('SO202607002', 1002, '浙江电子', 'EC', '浙江', 'offline',  95000.00,  62000.00,  8, false, 'completed', '2026-07-08', '2026-07-10'),
    ('SO202607003', 1003, '上海贸易', 'EC', '上海', 'online',   61000.00,  40000.00,  5, true,  'completed', '2026-07-15', '2026-07-16'),
    ('SO202607004', 2001, '广东制造', 'SC', '广东', 'online',   88000.00,  59000.00,  9, false, 'completed', '2026-07-20', '2026-07-22'),
    ('SO202607005', 3001, '北京科技', 'NC', '北京', 'offline',  54000.00,  35000.00,  4, true,  'completed', '2026-07-25', '2026-07-27'),
    ('SO202607006', 1001, '江苏机械', 'EC', '江苏', 'online',   30000.00,  20000.00,  3, false, 'cancelled', '2026-07-28', NULL),
    ('SO202608001', 1001, '江苏机械', 'EC', '江苏', 'online',  142000.00,  91000.00, 14, false, 'completed', '2026-08-02', '2026-08-04'),
    ('SO202608002', 1002, '浙江电子', 'EC', '浙江', 'offline', 110000.00,  71000.00, 10, false, 'completed', '2026-08-05', '2026-08-07'),
    ('SO202608003', 1004, '江苏精密', 'EC', '江苏', 'online',   47000.00,  31000.00,  4, true,  'completed', '2026-08-06', '2026-08-08'),
    ('SO202608004', 1003, '上海贸易', 'EC', '上海', 'online',   66000.00,  43000.00,  6, false, 'completed', '2026-08-09', '2026-08-10'),
    ('SO202608005', 2001, '广东制造', 'SC', '广东', 'online',   97000.00,  64000.00, 10, false, 'completed', '2026-08-03', '2026-08-05'),
    ('SO202608006', 2002, '深圳电器', 'SC', '广东', 'offline',  52000.00,  34000.00,  5, true,  'completed', '2026-08-11', '2026-08-12'),
    ('SO202608007', 3001, '北京科技', 'NC', '北京', 'offline',  58000.00,  38000.00,  5, false, 'completed', '2026-08-07', '2026-08-09'),
    ('SO202608008', 1002, '浙江电子', 'EC', '浙江', 'online',   41000.00,  27000.00,  4, false, 'pending',   '2026-08-12', NULL);
```

- [ ] **Step 2: 写建库脚本**

`backend/scripts/init_db.py`：

```python
"""Create schemas, metadata tables and load sample business data.

Run: python -m scripts.init_db
"""

from pathlib import Path

from sqlalchemy import text

from app.core.db import meta_engine, sample_engine
from app.semantic.orm import META_SCHEMA, Base

SAMPLE_SCHEMA = "sample"
SQL_FILE = Path(__file__).parent / "sample_data.sql"


def create_schemas() -> None:
    with meta_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{META_SCHEMA}"'))
    with sample_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SAMPLE_SCHEMA}"'))


def create_meta_tables() -> None:
    Base.metadata.create_all(meta_engine)


def load_sample_data() -> None:
    statements = SQL_FILE.read_text(encoding="utf-8")
    with sample_engine.begin() as conn:
        conn.execute(text(statements))


def main() -> None:
    create_schemas()
    create_meta_tables()
    load_sample_data()
    print("database initialised")


if __name__ == "__main__":
    main()
```

`backend/scripts/__init__.py` 留空。

- [ ] **Step 3: 写测试 fixture**

`backend/tests/conftest.py`：

```python
import pytest
from sqlalchemy.orm import Session

from app.core.db import meta_engine, sample_engine
from app.semantic.orm import Base
from scripts.init_db import create_schemas, load_sample_data


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    """Create schemas, tables and sample rows once per test session."""
    create_schemas()
    Base.metadata.create_all(meta_engine)
    load_sample_data()
    yield


@pytest.fixture
def meta_session() -> Session:
    """Metadata session rolled back after each test."""
    connection = meta_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def sample_conn():
    connection = sample_engine.connect()
    try:
        yield connection
    finally:
        connection.close()
```

- [ ] **Step 4: 写样本数据测试**

`backend/tests/test_sample_data.py`：

```python
from sqlalchemy import text


def test_sample_orders_loaded(sample_conn):
    total = sample_conn.execute(text("SELECT COUNT(*) FROM sample.orders")).scalar()
    assert total == 14


def test_sample_covers_two_months(sample_conn):
    months = sample_conn.execute(
        text(
            "SELECT DISTINCT date_trunc('month', completed_date)::date "
            "FROM sample.orders WHERE completed_date IS NOT NULL ORDER BY 1"
        )
    ).scalars().all()
    assert len(months) == 2


def test_sample_has_cancelled_and_pending_rows(sample_conn):
    # Needed so metric-level status filters are actually exercised.
    statuses = sample_conn.execute(
        text("SELECT DISTINCT status FROM sample.orders ORDER BY 1")
    ).scalars().all()
    assert statuses == ["cancelled", "completed", "pending"]
```

- [ ] **Step 5: 启动 PostgreSQL 并建库**

前置：本地需有可用 PostgreSQL 15+，且 `.env` 已按 `.env.example` 填好。

Run:
```bash
cd backend && cp -n .env.example .env && python -m scripts.init_db
```
Expected: 输出 `database initialised`

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_sample_data.py -v`
Expected: PASS（3 项）

- [ ] **Step 7: 提交**

```bash
git add backend/scripts backend/tests/conftest.py backend/tests/test_sample_data.py
git commit -F - <<'EOF'
增加样本订单数据与数据库初始化脚本

编译器与端到端测试都需要一份结果已知的业务数据，否则断言只能写成自证。样本数据覆盖跨月对比、区域筛选、省份分组、新客标记与非完成态订单，使指标口径与时间口径的差异能在测试中真实暴露。

- 建库脚本统一创建 agent_meta 与 sample 两个 schema 并载入样本数据
- 样本订单含两个月份与三种状态，支撑环比与状态过滤的验证
- 新增会话级建库 fixture 与函数级自动回滚的元数据会话 fixture
- 验证：python -m scripts.init_db 执行成功，pytest tests/test_sample_data.py 3 项通过
EOF
```

---

### Task 4: 不可变语义模型树与加载器

**Files:**
- Create: `backend/app/semantic/model.py`
- Create: `backend/app/semantic/loader.py`
- Create: `backend/tests/semantic/factories.py`
- Create: `backend/tests/semantic/test_loader.py`

**Interfaces:**
- Consumes: Task 2 的 ORM 类与枚举、Task 3 的 `meta_session` fixture
- Produces:
  - `app.semantic.model.FieldDef` — frozen dataclass，属性 `name/physical_column/business_name/synonyms/semantic_type/unit/display_format/default_aggregation/allowed_aggregations/is_filterable/is_groupable/is_queryable/sensitivity/enum_values`
  - `app.semantic.model.EnumValueDef` — frozen dataclass，属性 `physical_value/business_value/aliases/description`
  - `app.semantic.model.MetricDef` — frozen dataclass，属性 `name/business_name/synonyms/version/kind/aggregation_behavior/source_field/aggregation/fixed_filter/expression/time_field/unit/display_format/description`
  - `app.semantic.model.DatasetDef` — frozen dataclass，属性 `name/business_name/physical_table/aliases/description/grain/applicable_scenario/forbidden_scenario/is_published/fields/metrics/updated_at`
  - `DatasetDef.field(name) -> FieldDef`，缺失抛 `UnknownFieldError`
  - `DatasetDef.metric(name) -> MetricDef`，缺失抛 `UnknownMetricError`
  - `DatasetDef.resolve_enum(field_name, spoken) -> str | None` — 业务值/别名 → 物理值，命中不区分大小写
  - `app.semantic.model.UnknownFieldError`、`UnknownMetricError`（均继承 `SemanticError`）
  - `app.semantic.loader.load_dataset(session, name) -> DatasetDef`
  - `app.semantic.loader.list_datasets(session) -> list[DatasetDef]`
  - `tests.semantic.factories.build_orders_dataset(session) -> DatasetRow` — 写入与样本表匹配的完整语义配置并返回

- [ ] **Step 1: 写失败的加载器测试**

`backend/tests/semantic/test_loader.py`：

```python
import pytest

from app.semantic.enums import Aggregation, MetricKind
from app.semantic.loader import load_dataset
from app.semantic.model import UnknownFieldError, UnknownMetricError
from tests.semantic.factories import build_orders_dataset


def test_load_dataset_returns_fields_and_metrics(meta_session):
    build_orders_dataset(meta_session)

    dataset = load_dataset(meta_session, "orders")

    assert dataset.physical_table == "sample.orders"
    assert dataset.field("amount").semantic_type == "amount"
    assert dataset.metric("sales_revenue").kind == MetricKind.ATOMIC.value


def test_field_lookup_rejects_unknown_name(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(UnknownFieldError):
        dataset.field("no_such_field")


def test_metric_lookup_rejects_unknown_name(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(UnknownMetricError):
        dataset.metric("no_such_metric")


def test_resolve_enum_maps_business_value_to_physical(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert dataset.resolve_enum("region_code", "华东") == "EC"
    assert dataset.resolve_enum("region_code", "华东地区") == "EC"
    assert dataset.resolve_enum("region_code", "火星") is None


def test_allowed_aggregations_exclude_sum_for_ratio_like_fields(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert Aggregation.SUM.value in dataset.field("amount").allowed_aggregations
    assert Aggregation.SUM.value not in dataset.field("province").allowed_aggregations


def test_model_is_immutable(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(Exception):
        dataset.field("amount").business_name = "tampered"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/semantic/test_loader.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.semantic.model'`

- [ ] **Step 3: 写语义模型树**

`backend/app/semantic/model.py`：

```python
from dataclasses import dataclass, field as dc_field
from datetime import datetime


class SemanticError(Exception):
    """Base class for semantic-layer lookup failures."""


class UnknownFieldError(SemanticError):
    pass


class UnknownMetricError(SemanticError):
    pass


@dataclass(frozen=True, slots=True)
class EnumValueDef:
    physical_value: str
    business_value: str
    aliases: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class FieldDef:
    name: str
    physical_column: str
    semantic_type: str
    business_name: str = ""
    synonyms: tuple[str, ...] = ()
    unit: str = ""
    display_format: str = ""
    default_aggregation: str = "none"
    allowed_aggregations: tuple[str, ...] = ()
    is_filterable: bool = True
    is_groupable: bool = True
    is_queryable: bool = True
    sensitivity: str = "public"
    enum_values: tuple[EnumValueDef, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricDef:
    name: str
    business_name: str
    kind: str
    time_field: str
    version: int = 1
    synonyms: tuple[str, ...] = ()
    aggregation_behavior: str = "additive"
    source_field: str | None = None
    aggregation: str | None = None
    fixed_filter: str = ""
    expression: str = ""
    unit: str = ""
    display_format: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class DatasetDef:
    name: str
    business_name: str
    physical_table: str
    fields: tuple[FieldDef, ...]
    metrics: tuple[MetricDef, ...]
    aliases: tuple[str, ...] = ()
    description: str = ""
    grain: str = ""
    applicable_scenario: str = ""
    forbidden_scenario: str = ""
    is_published: bool = False
    updated_at: datetime | None = None

    def field(self, name: str) -> FieldDef:
        for item in self.fields:
            if item.name == name:
                return item
        raise UnknownFieldError(name)

    def metric(self, name: str) -> MetricDef:
        for item in self.metrics:
            if item.name == name:
                return item
        raise UnknownMetricError(name)

    def has_field(self, name: str) -> bool:
        return any(item.name == name for item in self.fields)

    def resolve_enum(self, field_name: str, spoken: str) -> str | None:
        """Map a spoken business value or alias to its physical value.

        Returns None when nothing matches; callers must raise clarification
        rather than silently querying an unmapped value (spec 4.3).
        """
        target = spoken.strip().casefold()
        for value in self.field(field_name).enum_values:
            candidates = (value.business_value, value.physical_value, *value.aliases)
            if any(candidate.casefold() == target for candidate in candidates):
                return value.physical_value
        return None
```

- [ ] **Step 4: 写加载器**

`backend/app/semantic/loader.py`：

```python
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.semantic.model import DatasetDef, EnumValueDef, FieldDef, MetricDef, SemanticError
from app.semantic.orm import DatasetRow, FieldRow, MetricRow


class UnknownDatasetError(SemanticError):
    pass


def _to_field(row: FieldRow) -> FieldDef:
    return FieldDef(
        name=row.name,
        physical_column=row.physical_column,
        semantic_type=row.semantic_type,
        business_name=row.business_name,
        synonyms=tuple(row.synonyms or ()),
        unit=row.unit,
        display_format=row.display_format,
        default_aggregation=row.default_aggregation,
        allowed_aggregations=tuple(row.allowed_aggregations or ()),
        is_filterable=row.is_filterable,
        is_groupable=row.is_groupable,
        is_queryable=row.is_queryable,
        sensitivity=row.sensitivity,
        enum_values=tuple(
            EnumValueDef(
                physical_value=value.physical_value,
                business_value=value.business_value,
                aliases=tuple(value.aliases or ()),
                description=value.description,
            )
            for value in row.enum_values
        ),
    )


def _to_metric(row: MetricRow) -> MetricDef:
    return MetricDef(
        name=row.name,
        business_name=row.business_name,
        kind=row.kind,
        time_field=row.time_field,
        version=row.version,
        synonyms=tuple(row.synonyms or ()),
        aggregation_behavior=row.aggregation_behavior,
        source_field=row.source_field,
        aggregation=row.aggregation,
        fixed_filter=row.fixed_filter,
        expression=row.expression,
        unit=row.unit,
        display_format=row.display_format,
        description=row.description,
    )


def _to_dataset(row: DatasetRow) -> DatasetDef:
    return DatasetDef(
        name=row.name,
        business_name=row.business_name,
        physical_table=row.physical_table,
        fields=tuple(_to_field(item) for item in row.fields),
        metrics=tuple(_to_metric(item) for item in row.metrics),
        aliases=tuple(row.aliases or ()),
        description=row.description,
        grain=row.grain,
        applicable_scenario=row.applicable_scenario,
        forbidden_scenario=row.forbidden_scenario,
        is_published=row.is_published,
        updated_at=row.updated_at,
    )


def _base_query():
    return select(DatasetRow).options(
        selectinload(DatasetRow.fields).selectinload(FieldRow.enum_values),
        selectinload(DatasetRow.metrics),
    )


def load_dataset(session: Session, name: str) -> DatasetDef:
    row = session.execute(_base_query().where(DatasetRow.name == name)).scalar_one_or_none()
    if row is None:
        raise UnknownDatasetError(name)
    return _to_dataset(row)


def list_datasets(session: Session) -> list[DatasetDef]:
    rows = session.execute(_base_query().order_by(DatasetRow.name)).scalars().all()
    return [_to_dataset(row) for row in rows]
```

- [ ] **Step 5: 写测试工厂**

`backend/tests/semantic/factories.py`。这份配置在后续所有计划的测试中复用，必须与 `sample.orders` 完全对应：

```python
"""Shared semantic configuration matching sample.orders."""

from sqlalchemy.orm import Session

from app.semantic.enums import (
    Aggregation,
    AggregationBehavior,
    MetricKind,
    SemanticType,
    Sensitivity,
)
from app.semantic.orm import DatasetRow, EnumValueRow, FieldRow, MetricRow

_NO_AGG: list[str] = []
_NUMERIC_AGGS = [
    Aggregation.SUM.value,
    Aggregation.AVG.value,
    Aggregation.MAX.value,
    Aggregation.MIN.value,
]


def build_orders_dataset(session: Session, *, published: bool = True) -> DatasetRow:
    dataset = DatasetRow(
        name="orders",
        business_name="订单",
        physical_table="sample.orders",
        aliases=["订单表", "销售订单"],
        description="订单粒度的销售明细",
        grain="一行一个订单",
        applicable_scenario="销售额、订单量、毛利分析",
        forbidden_scenario="不可用于财务确认收入口径",
        is_published=published,
    )

    dataset.fields = [
        FieldRow(
            name="order_id",
            physical_column="order_id",
            business_name="订单ID",
            semantic_type=SemanticType.ID.value,
            default_aggregation=Aggregation.NONE.value,
            allowed_aggregations=[Aggregation.COUNT.value, Aggregation.DISTINCT_COUNT.value],
            is_groupable=False,
        ),
        FieldRow(
            name="customer_id",
            physical_column="customer_id",
            business_name="客户ID",
            semantic_type=SemanticType.ID.value,
            allowed_aggregations=[Aggregation.DISTINCT_COUNT.value],
            is_groupable=False,
        ),
        FieldRow(
            name="customer_name",
            physical_column="customer_name",
            business_name="客户名称",
            semantic_type=SemanticType.TEXT.value,
            allowed_aggregations=_NO_AGG,
            sensitivity=Sensitivity.SENSITIVE.value,
        ),
        FieldRow(
            name="region_code",
            physical_column="region_code",
            business_name="大区",
            synonyms=["区域", "地区"],
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="EC", business_value="华东", aliases=["华东地区", "东区"]),
                EnumValueRow(physical_value="SC", business_value="华南", aliases=["华南地区", "南区"]),
                EnumValueRow(physical_value="NC", business_value="华北", aliases=["华北地区", "北区"]),
            ],
        ),
        FieldRow(
            name="province",
            physical_column="province",
            business_name="省份",
            semantic_type=SemanticType.TEXT.value,
            allowed_aggregations=_NO_AGG,
        ),
        FieldRow(
            name="channel",
            physical_column="channel",
            business_name="渠道",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="online", business_value="线上", aliases=["电商"]),
                EnumValueRow(physical_value="offline", business_value="线下", aliases=["门店"]),
            ],
        ),
        FieldRow(
            name="amount",
            physical_column="amount",
            business_name="订单金额",
            synonyms=["金额"],
            semantic_type=SemanticType.AMOUNT.value,
            unit="元",
            display_format="#,##0.00",
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="cost",
            physical_column="cost",
            business_name="订单成本",
            semantic_type=SemanticType.AMOUNT.value,
            unit="元",
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="quantity",
            physical_column="quantity",
            business_name="数量",
            semantic_type=SemanticType.QUANTITY.value,
            default_aggregation=Aggregation.SUM.value,
            allowed_aggregations=_NUMERIC_AGGS,
            is_groupable=False,
        ),
        FieldRow(
            name="is_new_customer",
            physical_column="is_new_customer",
            business_name="是否新客",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="true", business_value="新客"),
                EnumValueRow(physical_value="false", business_value="老客"),
            ],
        ),
        FieldRow(
            name="status",
            physical_column="status",
            business_name="订单状态",
            semantic_type=SemanticType.ENUM.value,
            allowed_aggregations=_NO_AGG,
            enum_values=[
                EnumValueRow(physical_value="completed", business_value="已完成"),
                EnumValueRow(physical_value="cancelled", business_value="已取消"),
                EnumValueRow(physical_value="pending", business_value="待处理"),
            ],
        ),
        FieldRow(
            name="created_date",
            physical_column="created_date",
            business_name="下单日期",
            semantic_type=SemanticType.DATE.value,
            allowed_aggregations=[Aggregation.MAX.value, Aggregation.MIN.value],
        ),
        FieldRow(
            name="completed_date",
            physical_column="completed_date",
            business_name="完成日期",
            semantic_type=SemanticType.DATE.value,
            allowed_aggregations=[Aggregation.MAX.value, Aggregation.MIN.value],
        ),
    ]

    dataset.metrics = [
        MetricRow(
            name="sales_revenue",
            business_name="销售额",
            synonyms=["营收", "销售收入"],
            version=3,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="amount",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="元",
            display_format="#,##0.00",
            owner="sales-ops",
            description="已完成订单含税金额",
        ),
        MetricRow(
            name="order_count",
            business_name="订单量",
            synonyms=["订单数"],
            version=1,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="order_id",
            aggregation=Aggregation.COUNT.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="单",
        ),
        MetricRow(
            name="new_customer_revenue",
            business_name="新客销售额",
            version=1,
            kind=MetricKind.DERIVED.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="amount",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed' AND is_new_customer = true",
            time_field="completed_date",
            unit="元",
        ),
        MetricRow(
            name="total_cost",
            business_name="总成本",
            version=1,
            kind=MetricKind.ATOMIC.value,
            aggregation_behavior=AggregationBehavior.ADDITIVE.value,
            source_field="cost",
            aggregation=Aggregation.SUM.value,
            fixed_filter="status = 'completed'",
            time_field="completed_date",
            unit="元",
        ),
        MetricRow(
            name="gross_margin_rate",
            business_name="毛利率",
            synonyms=["毛利"],
            version=2,
            kind=MetricKind.RATIO.value,
            # Spec 4.4: ratio metrics must be recalculated, never summed.
            aggregation_behavior=AggregationBehavior.RECALCULATE.value,
            expression="(sales_revenue - total_cost) / sales_revenue",
            time_field="completed_date",
            display_format="0.00%",
            description="(销售额 - 总成本) / 销售额",
        ),
    ]

    session.add(dataset)
    session.flush()
    return dataset
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/semantic/test_loader.py -v`
Expected: PASS（6 项）

- [ ] **Step 7: 提交**

```bash
git add backend/app/semantic/model.py backend/app/semantic/loader.py backend/tests/semantic/factories.py backend/tests/semantic/test_loader.py
git commit -F - <<'EOF'
实现不可变语义模型树与加载器

编译器必须读到一份不可变、无 ORM 依赖的语义视图，否则惰性加载与会话状态会让编译结果随调用时机变化。为此把元数据一次性物化成 frozen dataclass 树，并把枚举值映射收敛为模型自身的方法。

- 语义模型全部为 frozen dataclass，字段与指标查找失败时抛出显式异常而非返回空值
- 枚举值映射支持业务值与别名且不区分大小写，未命中返回空值以便上层转澄清
- 加载器用 selectinload 一次取全字段、枚举与指标，避免 N+1
- 新增与样本订单表完全对应的语义配置工厂，供后续各计划的测试复用
- 验证：pytest tests/semantic/test_loader.py 6 项通过
EOF
```

---

### Task 5: 语义体检（Semantic Lint）

**Files:**
- Create: `backend/app/semantic/lint.py`
- Create: `backend/tests/semantic/test_lint.py`

**Interfaces:**
- Consumes: `app.semantic.model.DatasetDef`、`app.semantic.enums.*`（Task 2、4）
- Produces:
  - `app.semantic.lint.LintSeverity` — `ERROR/WARNING`
  - `app.semantic.lint.LintIssue` — frozen dataclass，属性 `code: str`、`severity: str`、`target: str`、`message: str`
  - `app.semantic.lint.lint_dataset(dataset: DatasetDef) -> list[LintIssue]`
  - `app.semantic.lint.is_publishable(dataset: DatasetDef) -> bool` — 无 ERROR 即可发布
  - 检查项编码：`FIELD_NO_BUSINESS_NAME`、`ENUM_NO_DICTIONARY`、`METRIC_NO_TIME_FIELD`、`METRIC_BAD_FIELD_REF`、`METRIC_BAD_METRIC_REF`、`METRIC_AGG_NOT_ALLOWED`、`RATIO_METRIC_ADDITIVE`、`DATASET_NO_GRAIN`

- [ ] **Step 1: 写失败的体检测试**

`backend/tests/semantic/test_lint.py`：

```python
from dataclasses import replace

from app.semantic.enums import Aggregation, AggregationBehavior, MetricKind, SemanticType
from app.semantic.lint import is_publishable, lint_dataset
from app.semantic.loader import load_dataset
from app.semantic.model import DatasetDef, FieldDef, MetricDef
from tests.semantic.factories import build_orders_dataset


def _codes(dataset: DatasetDef) -> set[str]:
    return {issue.code for issue in lint_dataset(dataset)}


def test_well_configured_dataset_passes(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert lint_dataset(dataset) == []
    assert is_publishable(dataset) is True


def test_field_without_business_name_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_fields = tuple(
        replace(item, business_name="") if item.name == "amount" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=broken_fields)

    assert "FIELD_NO_BUSINESS_NAME" in _codes(dataset)
    assert is_publishable(dataset) is False


def test_enum_field_without_dictionary_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_fields = tuple(
        replace(item, enum_values=()) if item.name == "region_code" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=broken_fields)

    assert "ENUM_NO_DICTIONARY" in _codes(dataset)


def test_metric_referencing_missing_field_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, source_field="deleted_column") if item.name == "sales_revenue" else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_BAD_FIELD_REF" in _codes(dataset)


def test_metric_referencing_missing_metric_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, expression="(sales_revenue - ghost_metric) / sales_revenue")
        if item.name == "gross_margin_rate"
        else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_BAD_METRIC_REF" in _codes(dataset)


def test_metric_with_disallowed_aggregation_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    # province does not allow SUM; a metric summing it must be rejected.
    broken_metrics = (
        MetricDef(
            name="bad_metric",
            business_name="错误指标",
            kind=MetricKind.ATOMIC.value,
            time_field="completed_date",
            source_field="province",
            aggregation=Aggregation.SUM.value,
        ),
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_AGG_NOT_ALLOWED" in _codes(dataset)


def test_ratio_metric_marked_additive_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, aggregation_behavior=AggregationBehavior.ADDITIVE.value)
        if item.name == "gross_margin_rate"
        else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "RATIO_METRIC_ADDITIVE" in _codes(dataset)


def test_metric_time_field_must_be_a_date_field(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, time_field="province") if item.name == "sales_revenue" else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_NO_TIME_FIELD" in _codes(dataset)


def test_dataset_without_grain_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    dataset = replace(dataset, grain="")

    assert "DATASET_NO_GRAIN" in _codes(dataset)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/semantic/test_lint.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.semantic.lint'`

- [ ] **Step 3: 写体检模块**

`backend/app/semantic/lint.py`：

```python
"""Semantic lint (spec M-07).

A dataset with any ERROR-level issue must not be usable for querying.
This is the first quality gate: misconfigured semantics do not raise errors
at query time, they return plausible wrong numbers.
"""

import re
from dataclasses import dataclass
from enum import Enum

from app.semantic.enums import AggregationBehavior, MetricKind, SemanticType
from app.semantic.model import DatasetDef

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Tokens that may appear in a composite/ratio expression without being a metric.
_EXPRESSION_KEYWORDS = frozenset({"nullif", "coalesce", "case", "when", "then", "else", "end"})


class LintSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class LintIssue:
    code: str
    severity: str
    target: str
    message: str


def _check_dataset(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    if not dataset.grain.strip():
        issues.append(
            LintIssue(
                code="DATASET_NO_GRAIN",
                severity=LintSeverity.WARNING.value,
                target=dataset.name,
                message="数据集未声明粒度，问数时无法判断是否需要去重",
            )
        )
    return issues


def _check_fields(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for item in dataset.fields:
        if not item.business_name.strip():
            issues.append(
                LintIssue(
                    code="FIELD_NO_BUSINESS_NAME",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="字段缺少业务名，意图识别无法命中该字段",
                )
            )
        if item.semantic_type == SemanticType.ENUM.value and not item.enum_values:
            issues.append(
                LintIssue(
                    code="ENUM_NO_DICTIONARY",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="枚举字段缺少值字典，用户说业务值时必然查空",
                )
            )
        if item.default_aggregation != "none" and (
            item.default_aggregation not in item.allowed_aggregations
        ):
            issues.append(
                LintIssue(
                    code="METRIC_AGG_NOT_ALLOWED",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="字段默认聚合不在允许聚合列表内",
                )
            )
    return issues


def _check_metric_time_field(dataset: DatasetDef, metric) -> list[LintIssue]:
    if not metric.time_field.strip() or not dataset.has_field(metric.time_field):
        return [
            LintIssue(
                code="METRIC_NO_TIME_FIELD",
                severity=LintSeverity.ERROR.value,
                target=f"{dataset.name}.{metric.name}",
                message="指标未声明有效的时间口径字段",
            )
        ]
    if dataset.field(metric.time_field).semantic_type != SemanticType.DATE.value:
        return [
            LintIssue(
                code="METRIC_NO_TIME_FIELD",
                severity=LintSeverity.ERROR.value,
                target=f"{dataset.name}.{metric.name}",
                message="指标的时间口径字段不是日期类型",
            )
        ]
    return []


def _check_metrics(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    metric_names = {item.name for item in dataset.metrics}

    for metric in dataset.metrics:
        target = f"{dataset.name}.{metric.name}"
        issues.extend(_check_metric_time_field(dataset, metric))

        if metric.kind in (MetricKind.ATOMIC.value, MetricKind.DERIVED.value):
            if not metric.source_field or not dataset.has_field(metric.source_field):
                issues.append(
                    LintIssue(
                        code="METRIC_BAD_FIELD_REF",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message="指标引用了不存在的字段",
                    )
                )
            elif metric.aggregation not in dataset.field(metric.source_field).allowed_aggregations:
                issues.append(
                    LintIssue(
                        code="METRIC_AGG_NOT_ALLOWED",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message=f"字段 {metric.source_field} 不允许 {metric.aggregation} 聚合",
                    )
                )

        if metric.kind in (MetricKind.COMPOSITE.value, MetricKind.RATIO.value):
            referenced = {
                token
                for token in _IDENTIFIER_RE.findall(metric.expression)
                if token.casefold() not in _EXPRESSION_KEYWORDS
            }
            missing = referenced - metric_names
            if missing:
                issues.append(
                    LintIssue(
                        code="METRIC_BAD_METRIC_REF",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message=f"指标表达式引用了不存在的指标：{', '.join(sorted(missing))}",
                    )
                )

        if (
            metric.kind == MetricKind.RATIO.value
            and metric.aggregation_behavior != AggregationBehavior.RECALCULATE.value
        ):
            issues.append(
                LintIssue(
                    code="RATIO_METRIC_ADDITIVE",
                    severity=LintSeverity.ERROR.value,
                    target=target,
                    message="比率指标必须标注为 recalculate，否则汇总与下钻必然算错",
                )
            )

    return issues


def lint_dataset(dataset: DatasetDef) -> list[LintIssue]:
    return [
        *_check_dataset(dataset),
        *_check_fields(dataset),
        *_check_metrics(dataset),
    ]


def is_publishable(dataset: DatasetDef) -> bool:
    """A dataset is publishable when it has no ERROR-level issue."""
    return not any(
        issue.severity == LintSeverity.ERROR.value for issue in lint_dataset(dataset)
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/semantic/test_lint.py -v`
Expected: PASS（9 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/semantic/lint.py backend/tests/semantic/test_lint.py
git commit -F - <<'EOF'
实现语义体检并作为发布闸门

语义配错不会在查询时报错，只会返回看起来合理的错数字，因此必须在发布前静态检出。体检把八类配置缺陷归为错误或警告两级，仅错误级阻断发布，避免把粒度缺失这类可容忍问题也变成硬阻塞。

- 检出字段无业务名、枚举无字典、指标引用已删字段或指标、聚合越权、时间口径无效
- 比率指标未标注 recalculate 判定为错误，防止汇总下钻时被求和
- is_publishable 仅在无错误级问题时放行，作为问答链路的准入条件
- 验证：pytest tests/semantic/test_lint.py 9 项通过
EOF
```

---

### Task 6: 语义配置 API

**Files:**
- Create: `backend/app/semantic/schemas.py`
- Create: `backend/app/semantic/service.py`
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/semantic.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/__init__.py`
- Create: `backend/tests/api/test_semantic_api.py`

**Interfaces:**
- Consumes: Task 4 的 `load_dataset`/`list_datasets`、Task 5 的 `lint_dataset`/`is_publishable`
- Produces:
  - `app.semantic.schemas.DatasetSummaryOut`、`DatasetDetailOut`、`FieldOut`、`EnumValueOut`、`MetricOut`、`LintIssueOut`、`LintReportOut`、`PublishResultOut`
  - `app.semantic.service.get_lint_report(session, name) -> LintReportOut`
  - `app.semantic.service.publish_dataset(session, name) -> PublishResultOut` — 体检不通过则不改 `is_published` 并返回失败
  - REST 路由：`GET /api/semantic/datasets`、`GET /api/semantic/datasets/{name}`、`GET /api/semantic/datasets/{name}/lint`、`POST /api/semantic/datasets/{name}/publish`
  - `app.main.app` 挂载 `app.api.semantic.router`

- [ ] **Step 1: 写失败的 API 测试**

`backend/tests/api/test_semantic_api.py`：

```python
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session
from app.main import app
from app.semantic.orm import DatasetRow
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def client(meta_session):
    app.dependency_overrides[get_meta_session] = lambda: meta_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_datasets(client, meta_session):
    build_orders_dataset(meta_session)

    response = client.get("/api/semantic/datasets")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "orders" in names


def test_dataset_detail_includes_metrics_and_enum_values(client, meta_session):
    build_orders_dataset(meta_session)

    body = client.get("/api/semantic/datasets/orders").json()

    assert body["forbidden_scenario"].startswith("不可用于")
    metric_names = [item["name"] for item in body["metrics"]]
    assert "gross_margin_rate" in metric_names
    region = next(item for item in body["fields"] if item["name"] == "region_code")
    assert {value["business_value"] for value in region["enum_values"]} == {"华东", "华南", "华北"}


def test_unknown_dataset_returns_404(client):
    response = client.get("/api/semantic/datasets/ghost")
    assert response.status_code == 404


def test_lint_report_is_clean_for_valid_dataset(client, meta_session):
    build_orders_dataset(meta_session)

    body = client.get("/api/semantic/datasets/orders/lint").json()

    assert body["publishable"] is True
    assert body["issues"] == []


def test_publish_blocked_when_lint_fails(client, meta_session):
    dataset = build_orders_dataset(meta_session, published=False)
    # Break one field so lint produces an ERROR.
    dataset.fields[0].business_name = ""
    meta_session.flush()

    response = client.post("/api/semantic/datasets/orders/publish")

    assert response.status_code == 409
    stored = meta_session.get(DatasetRow, dataset.id)
    assert stored.is_published is False


def test_publish_succeeds_when_lint_passes(client, meta_session):
    dataset = build_orders_dataset(meta_session, published=False)

    response = client.post("/api/semantic/datasets/orders/publish")

    assert response.status_code == 200
    assert response.json()["published"] is True
    stored = meta_session.get(DatasetRow, dataset.id)
    assert stored.is_published is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/api/test_semantic_api.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.semantic.schemas'`

- [ ] **Step 3: 写响应模型**

`backend/app/semantic/schemas.py`：

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EnumValueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    physical_value: str
    business_value: str
    aliases: list[str]
    description: str


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    physical_column: str
    business_name: str
    synonyms: list[str]
    semantic_type: str
    unit: str
    display_format: str
    default_aggregation: str
    allowed_aggregations: list[str]
    is_filterable: bool
    is_groupable: bool
    is_queryable: bool
    sensitivity: str
    enum_values: list[EnumValueOut]


class MetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    synonyms: list[str]
    version: int
    kind: str
    aggregation_behavior: str
    source_field: str | None
    aggregation: str | None
    fixed_filter: str
    expression: str
    time_field: str
    unit: str
    display_format: str
    description: str


class DatasetSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    business_name: str
    physical_table: str
    grain: str
    is_published: bool
    updated_at: datetime | None


class DatasetDetailOut(DatasetSummaryOut):
    aliases: list[str]
    description: str
    applicable_scenario: str
    forbidden_scenario: str
    fields: list[FieldOut]
    metrics: list[MetricOut]


class LintIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    severity: str
    target: str
    message: str


class LintReportOut(BaseModel):
    dataset: str
    publishable: bool
    issues: list[LintIssueOut]


class PublishResultOut(BaseModel):
    dataset: str
    published: bool
    issues: list[LintIssueOut]
```

- [ ] **Step 4: 写服务层**

`backend/app/semantic/service.py`：

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.semantic.lint import LintSeverity, lint_dataset
from app.semantic.loader import load_dataset
from app.semantic.orm import DatasetRow
from app.semantic.schemas import LintIssueOut, LintReportOut, PublishResultOut


def get_lint_report(session: Session, name: str) -> LintReportOut:
    dataset = load_dataset(session, name)
    issues = lint_dataset(dataset)
    return LintReportOut(
        dataset=name,
        publishable=not any(item.severity == LintSeverity.ERROR.value for item in issues),
        issues=[LintIssueOut.model_validate(item) for item in issues],
    )


def publish_dataset(session: Session, name: str) -> PublishResultOut:
    """Flip is_published only when lint reports no ERROR (spec M-07)."""
    report = get_lint_report(session, name)
    if not report.publishable:
        return PublishResultOut(dataset=name, published=False, issues=report.issues)

    row = session.execute(
        select(DatasetRow).where(DatasetRow.name == name)
    ).scalar_one()
    row.is_published = True
    session.flush()
    return PublishResultOut(dataset=name, published=True, issues=[])
```

- [ ] **Step 5: 写路由**

`backend/app/api/semantic.py`：

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_meta_session
from app.semantic.loader import list_datasets, load_dataset
from app.semantic.model import SemanticError
from app.semantic.schemas import (
    DatasetDetailOut,
    DatasetSummaryOut,
    LintReportOut,
    PublishResultOut,
)
from app.semantic.service import get_lint_report, publish_dataset

router = APIRouter(prefix="/api/semantic", tags=["semantic"])


@router.get("/datasets", response_model=list[DatasetSummaryOut])
def get_datasets(session: Session = Depends(get_meta_session)):
    return [DatasetSummaryOut.model_validate(item) for item in list_datasets(session)]


@router.get("/datasets/{name}", response_model=DatasetDetailOut)
def get_dataset(name: str, session: Session = Depends(get_meta_session)):
    try:
        dataset = load_dataset(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    return DatasetDetailOut.model_validate(dataset)


@router.get("/datasets/{name}/lint", response_model=LintReportOut)
def get_dataset_lint(name: str, session: Session = Depends(get_meta_session)):
    try:
        return get_lint_report(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")


@router.post("/datasets/{name}/publish", response_model=PublishResultOut)
def post_dataset_publish(name: str, session: Session = Depends(get_meta_session)):
    try:
        result = publish_dataset(session, name)
    except SemanticError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据集不存在")
    if not result.published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "语义体检未通过，无法发布", "issues": [
                item.model_dump() for item in result.issues
            ]},
        )
    return result
```

`backend/app/api/__init__.py`、`backend/tests/api/__init__.py` 留空。

- [ ] **Step 6: 挂载路由**

`backend/app/main.py` 中，在 `health` 函数定义之前加入：

```python
from app.api.semantic import router as semantic_router

app.include_router(semantic_router)
```

导入语句置于文件顶部的 import 区，`app.include_router(...)` 置于中间件配置之后。

- [ ] **Step 7: 运行全部测试确认通过**

Run: `cd backend && python -m pytest -v`
Expected: PASS（Task 1~6 累计 25 项）

- [ ] **Step 8: 提交**

```bash
git add backend/app/semantic/schemas.py backend/app/semantic/service.py backend/app/api backend/app/main.py backend/tests/api
git commit -F - <<'EOF'
增加语义配置查询与发布接口

前端配置页与问答链路都需要读取语义模型，且发布动作必须与体检结果强绑定，不能由调用方自行决定是否校验。因此把发布逻辑收在服务层，仅在无错误级问题时改写发布状态。

- 提供数据集列表、详情、体检报告三个查询接口
- 发布接口在体检未通过时返回 409 并回传问题清单，且不修改发布状态
- 响应模型显式暴露禁用场景与指标版本，供答案引证使用
- 验证：pytest 全量 25 项通过
EOF
```

---

## 自查

**Spec 覆盖**（对应设计文档 4.1~4.6、7）：

| Spec 条目 | 承载任务 |
|---|---|
| 4.1 数据集语义（含禁用场景） | Task 2、4、6 |
| 4.2 字段语义（本轮生效属性 + 保留列） | Task 2、4 |
| 4.3 枚举值字典（独立查询、不进 Prompt） | Task 2、4（`resolve_enum`） |
| 4.4 四类指标 + 版本 + 时间口径 | Task 2、4、5 |
| 4.5 时间计算内置模板 | 计划 02（编译器职责） |
| 4.6 语义体检 + 发布闸门 | Task 5、6 |
| 7 PostgreSQL + 样本数据集 | Task 1、3 |

4.5 不在本计划内，由计划 02 承载——它是编译器的行为，不是语义存储的行为。

**类型一致性**：`FieldDef.allowed_aggregations` 在 Task 4 定义为 `tuple[str, ...]`，Task 5 的 `in` 判断与 Task 6 的 `list[str]` 序列化均兼容；`LintIssue` 的四个属性名与 `LintIssueOut` 一致；`build_orders_dataset` 的签名 `(session, *, published=True)` 在 Task 5、6 的调用处一致。

**依赖顺序**：Task 1 → 2 → 3 → 4 → 5 → 6，每个任务结束时可独立运行测试。

## 交付物

完成本计划后：后端可启动，语义模型可从数据库加载为不可变树，体检可阻断不合格数据集发布，配置接口可供前端与后续计划消费。**此时还不能问数**——意图与编译器在计划 02。

