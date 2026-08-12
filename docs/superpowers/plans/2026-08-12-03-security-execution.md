# 安全改写与执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在编译产物上完成行级权限注入、列权限与脱敏、AST 白名单终检、成本护栏，然后执行并校验结果。

**Architecture:** 安全改写是一条作用在 sqlglot AST 上的变换流水线：`CompiledQuery` 进，`SecuredQuery` 出。每一道变换都是纯函数，可单独测试。白名单终检放在流水线末端，检查的是**将要真正执行的那棵树**，而不是编译刚产出的树——否则改写本身引入的问题检不出来。执行层单独一层，只负责发送 SQL 与取回结果，不做任何 SQL 变换。

**Tech Stack:** Python 3.11、sqlglot 25.x、SQLAlchemy 2.0、PostgreSQL 15+、pytest

## Global Constraints

以下约束来自 `docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md`，每个任务的要求都隐含包含本节：

- **权限不由模型实现**。RLS、列权限、脱敏全部是代码层面的确定性改写，任何环节都不依赖告知模型「你不能查这个」。
- **所有查询路径都必须过安全改写**，包括 Verified Query 的固定 SQL。存在任何绕过路径即视为越权通道。
- **越权拒答不得泄漏元数据**。错误信息只能是「你没有该数据的访问权限」，不能出现表名、列名、数据集名。
- AST 白名单是白名单而非黑名单：只允许 SELECT，出现任何非许可节点即拒绝。
- 结果集必须有强制上限，编译器未给 LIMIT 时由改写层补齐。
- 安全相关测试必须 100% 通过，这是发布门禁。
- 代码注释与标识符用英文；文档与提交信息用中文。

## 前置

依赖计划 01（`app.semantic.*`、`tests.semantic.factories`、样本库 `sample.orders`）与计划 02（`app.compiler.query.CompiledQuery`、`app.intent.schema`）。

---

### Task 1: 权限主体与策略模型

**Files:**
- Create: `backend/app/security/__init__.py`
- Create: `backend/app/security/orm.py`
- Create: `backend/app/security/principal.py`
- Create: `backend/tests/security/__init__.py`
- Create: `backend/tests/security/test_principal.py`
- Create: `backend/tests/security/factories.py`

**Interfaces:**
- Consumes: `app.core.db.MetaBase`、`app.semantic.enums.Sensitivity`
- Produces:
  - `app.security.orm.RoleRow`(`id`/`name`/`business_name`/`max_sensitivity`) — `agent_meta.roles`
  - `app.security.orm.UserRow`(`id`/`username`/`display_name`/`is_active`/`roles`) — `agent_meta.users`
  - `app.security.orm.RowPolicyRow`(`id`/`role_id`/`dataset_name`/`field_name`/`operator`/`values`) — `agent_meta.row_policies`
  - `app.security.orm.ColumnPolicyRow`(`id`/`role_id`/`dataset_name`/`field_name`/`access`) — `agent_meta.column_policies`
  - `app.security.principal.ColumnAccess` — 枚举 `ALLOW`/`MASK`/`DENY`
  - `app.security.principal.RowRule` — frozen dataclass `dataset_name`/`field_name`/`operator`/`values`
  - `app.security.principal.Principal` — frozen dataclass `user_id`/`username`/`role_names`/`max_sensitivity`/`row_rules`/`column_overrides`；方法 `row_rules_for(dataset_name)`、`column_access(field)`
  - `app.security.principal.load_principal(session, username) -> Principal`

- [ ] **Step 1: 写失败的权限主体测试**

`backend/tests/security/test_principal.py`：

```python
import pytest

from app.security.principal import ColumnAccess, load_principal
from app.semantic.enums import Sensitivity
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def principals(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def test_load_principal_collects_roles(principals):
    principal = load_principal(principals, "east_manager")
    assert "east_sales" in principal.role_names


def test_row_rules_are_scoped_to_dataset(principals):
    principal = load_principal(principals, "east_manager")
    rules = principal.row_rules_for("orders")

    assert len(rules) == 1
    assert rules[0].field_name == "region_code"
    assert rules[0].values == ("EC",)
    assert principal.row_rules_for("unknown_dataset") == ()


def test_admin_has_no_row_rules(principals):
    principal = load_principal(principals, "admin")
    assert principal.row_rules_for("orders") == ()


def test_sensitivity_ceiling_masks_higher_levels(principals):
    dataset = load_dataset(principals, "orders")
    analyst = load_principal(principals, "analyst")

    # analyst tops out at PUBLIC, so a SENSITIVE column is not readable as-is.
    assert analyst.max_sensitivity == Sensitivity.PUBLIC
    assert analyst.column_access(dataset.field("customer_name")) == ColumnAccess.MASK
    assert analyst.column_access(dataset.field("amount")) == ColumnAccess.ALLOW


def test_admin_reads_sensitive_columns(principals):
    dataset = load_dataset(principals, "orders")
    admin = load_principal(principals, "admin")
    assert admin.column_access(dataset.field("customer_name")) == ColumnAccess.ALLOW


def test_explicit_column_policy_overrides_sensitivity(principals):
    dataset = load_dataset(principals, "orders")
    # east_manager clears the sensitivity bar but cost is explicitly denied.
    principal = load_principal(principals, "east_manager")

    assert principal.column_access(dataset.field("customer_name")) == ColumnAccess.ALLOW
    assert principal.column_access(dataset.field("cost")) == ColumnAccess.DENY


def test_most_permissive_role_wins_on_sensitivity(principals):
    # multi_role belongs to both analyst (PUBLIC) and east_sales (SENSITIVE).
    principal = load_principal(principals, "multi_role")
    assert principal.max_sensitivity == Sensitivity.SENSITIVE


def test_row_rules_from_multiple_roles_are_all_collected(principals):
    principal = load_principal(principals, "multi_role")
    fields = {rule.field_name for rule in principal.row_rules_for("orders")}
    assert fields == {"region_code", "channel"}


def test_inactive_user_cannot_be_loaded(principals):
    from app.security.principal import PrincipalNotFoundError

    with pytest.raises(PrincipalNotFoundError):
        load_principal(principals, "retired_user")


def test_unknown_user_raises(principals):
    from app.security.principal import PrincipalNotFoundError

    with pytest.raises(PrincipalNotFoundError):
        load_principal(principals, "nobody")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_principal.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security'`

- [ ] **Step 3: 写权限 ORM**

`backend/app/security/orm.py`：

```python
"""Users, roles and permission policies.

Policies live in metadata, never in prompts: row and column restrictions are
applied by rewriting SQL, so the model is never asked to respect them.
"""

from sqlalchemy import Boolean, ForeignKey, String, Table, Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import META_SCHEMA, MetaBase
from app.semantic.enums import Sensitivity

user_roles = Table(
    "user_roles",
    MetaBase.metadata,
    Column("user_id", ForeignKey(f"{META_SCHEMA}.users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey(f"{META_SCHEMA}.roles.id", ondelete="CASCADE"), primary_key=True),
    schema=META_SCHEMA,
)


class RoleRow(MetaBase):
    __tablename__ = "roles"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    business_name: Mapped[str] = mapped_column(String(128), default="")
    # Highest column sensitivity this role may read unmasked.
    max_sensitivity: Mapped[str] = mapped_column(String(32), default=Sensitivity.PUBLIC.value)

    row_policies: Mapped[list["RowPolicyRow"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    column_policies: Mapped[list["ColumnPolicyRow"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class UserRow(MetaBase):
    __tablename__ = "users"
    __table_args__ = {"schema": META_SCHEMA}

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    roles: Mapped[list[RoleRow]] = relationship(secondary=user_roles)


class RowPolicyRow(MetaBase):
    """One row-level restriction: dataset.field must match values."""

    __tablename__ = "row_policies"
    __table_args__ = (
        UniqueConstraint("role_id", "dataset_name", "field_name"),
        {"schema": META_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.roles.id", ondelete="CASCADE")
    )
    dataset_name: Mapped[str] = mapped_column(String(64))
    field_name: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(16), default="in")
    values: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    role: Mapped[RoleRow] = relationship(back_populates="row_policies")


class ColumnPolicyRow(MetaBase):
    """Explicit per-column decision, overriding the sensitivity ceiling."""

    __tablename__ = "column_policies"
    __table_args__ = (
        UniqueConstraint("role_id", "dataset_name", "field_name"),
        {"schema": META_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey(f"{META_SCHEMA}.roles.id", ondelete="CASCADE")
    )
    dataset_name: Mapped[str] = mapped_column(String(64))
    field_name: Mapped[str] = mapped_column(String(64))
    access: Mapped[str] = mapped_column(String(16), default="allow")

    role: Mapped[RoleRow] = relationship(back_populates="column_policies")
```

- [ ] **Step 4: 写权限主体**

`backend/app/security/principal.py`：

```python
"""The resolved permission view for one user.

Roles are unioned: the widest sensitivity ceiling wins, while row rules from
every role are collected and later ANDed — a user who is scoped to East China
by one role does not escape that scope by holding a second role.
"""

from dataclasses import dataclass, field as dataclass_field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.security.orm import RoleRow, UserRow
from app.semantic.enums import Sensitivity
from app.semantic.model import FieldDef

_SENSITIVITY_ORDER = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.CONFIDENTIAL: 3,
}


class PrincipalNotFoundError(Exception):
    """Raised when no active user matches. Carries no metadata by design."""


class ColumnAccess(str, Enum):
    ALLOW = "allow"
    MASK = "mask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RowRule:
    dataset_name: str
    field_name: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    username: str
    role_names: tuple[str, ...] = ()
    max_sensitivity: Sensitivity = Sensitivity.PUBLIC
    row_rules: tuple[RowRule, ...] = ()
    # (dataset_name, field_name) -> explicit decision
    column_overrides: dict[tuple[str, str], ColumnAccess] = dataclass_field(default_factory=dict)

    def row_rules_for(self, dataset_name: str) -> tuple[RowRule, ...]:
        return tuple(rule for rule in self.row_rules if rule.dataset_name == dataset_name)

    def column_access(self, field: FieldDef, dataset_name: str = "") -> ColumnAccess:
        """Explicit policy first, sensitivity ceiling second."""
        override = self.column_overrides.get((dataset_name, field.name))
        if override is None:
            # Policies are stored per dataset; fall back to a dataset-agnostic key
            # so callers that only hold a field can still be answered.
            override = next(
                (
                    value
                    for (_, name), value in self.column_overrides.items()
                    if name == field.name
                ),
                None,
            )
        if override is not None:
            return override

        if _SENSITIVITY_ORDER[field.sensitivity] <= _SENSITIVITY_ORDER[self.max_sensitivity]:
            return ColumnAccess.ALLOW
        return ColumnAccess.MASK


def _widest(values: list[Sensitivity]) -> Sensitivity:
    return max(values, key=lambda item: _SENSITIVITY_ORDER[item], default=Sensitivity.PUBLIC)


def load_principal(session: Session, username: str) -> Principal:
    statement = (
        select(UserRow)
        .where(UserRow.username == username, UserRow.is_active.is_(True))
        .options(
            selectinload(UserRow.roles).selectinload(RoleRow.row_policies),
            selectinload(UserRow.roles).selectinload(RoleRow.column_policies),
        )
    )
    user = session.execute(statement).scalar_one_or_none()
    if user is None:
        raise PrincipalNotFoundError("用户不存在或已停用")

    rules: list[RowRule] = []
    overrides: dict[tuple[str, str], ColumnAccess] = {}
    for role in user.roles:
        for policy in role.row_policies:
            rules.append(
                RowRule(
                    dataset_name=policy.dataset_name,
                    field_name=policy.field_name,
                    operator=policy.operator,
                    values=tuple(policy.values),
                )
            )
        for policy in role.column_policies:
            key = (policy.dataset_name, policy.field_name)
            access = ColumnAccess(policy.access)
            existing = overrides.get(key)
            # DENY is not escapable by holding another role.
            if existing is None or access == ColumnAccess.DENY:
                overrides[key] = access

    return Principal(
        user_id=user.id,
        username=user.username,
        role_names=tuple(role.name for role in user.roles),
        max_sensitivity=_widest([Sensitivity(role.max_sensitivity) for role in user.roles]),
        row_rules=tuple(rules),
        column_overrides=overrides,
    )
```

- [ ] **Step 5: 写权限测试工厂**

`backend/tests/security/factories.py`：

```python
"""Principals used across the security tests.

- admin: unrestricted
- east_manager: rows scoped to EC, cost column explicitly denied
- analyst: PUBLIC ceiling only, so sensitive columns get masked
- multi_role: analyst + east_sales, to test role union
- retired_user: inactive
"""

from sqlalchemy.orm import Session

from app.security.orm import ColumnPolicyRow, RoleRow, RowPolicyRow, UserRow
from app.semantic.enums import Sensitivity


def build_principals(session: Session) -> dict[str, UserRow]:
    admin_role = RoleRow(
        name="admin",
        business_name="管理员",
        max_sensitivity=Sensitivity.CONFIDENTIAL.value,
    )
    east_role = RoleRow(
        name="east_sales",
        business_name="华东销售",
        max_sensitivity=Sensitivity.SENSITIVE.value,
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="region_code", operator="in", values=["EC"]
            )
        ],
        column_policies=[
            ColumnPolicyRow(dataset_name="orders", field_name="cost", access="deny")
        ],
    )
    analyst_role = RoleRow(
        name="analyst",
        business_name="分析师",
        max_sensitivity=Sensitivity.PUBLIC.value,
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="channel", operator="in", values=["online"]
            )
        ],
    )

    users = {
        "admin": UserRow(username="admin", display_name="管理员", roles=[admin_role]),
        "east_manager": UserRow(
            username="east_manager", display_name="华东负责人", roles=[east_role]
        ),
        "analyst": UserRow(username="analyst", display_name="分析师", roles=[analyst_role]),
        "multi_role": UserRow(
            username="multi_role", display_name="双角色", roles=[analyst_role, east_role]
        ),
        "retired_user": UserRow(
            username="retired_user", display_name="已离职", is_active=False, roles=[analyst_role]
        ),
    }

    session.add_all(users.values())
    session.flush()
    return users
```

- [ ] **Step 6: 在建库脚本中注册新表**

`backend/scripts/init_db.py` 中，与语义 ORM 并列导入权限 ORM，确保 `MetaBase.metadata.create_all` 覆盖新表：

```python
# Imported for the side effect of registering tables on MetaBase.metadata.
from app.security import orm as security_orm  # noqa: F401
from app.semantic import orm as semantic_orm  # noqa: F401
```

`backend/tests/conftest.py` 的 `prepared_database` fixture 同样需要导入该模块，否则测试库缺表。

- [ ] **Step 7: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_principal.py -v`
Expected: PASS（10 项）

- [ ] **Step 8: 提交**

```bash
git add backend/app/security backend/tests/security backend/scripts/init_db.py backend/tests/conftest.py
git commit -F - <<'EOF'
实现用户角色与行列权限策略模型

权限必须由代码强制而非告知模型，因此策略需要有自己的元数据结构，且多角色叠加时的取值规则要明确：宽松原则用于敏感级别上限，收紧原则用于行范围与列拒绝，否则多持一个角色就能绕过限制。

- 新增角色、用户、行策略、列策略四张元数据表
- 敏感级别上限取所有角色中最宽的一档，行规则收集全部角色后由改写层合并
- 列策略显式拒绝优先于敏感级别推导，且不因另一角色放行而失效
- 主体加载失败的异常不携带任何表名或字段名
- 验证：pytest tests/security/test_principal.py 10 项通过
EOF
```

---

### Task 2: 行级权限注入

**Files:**
- Create: `backend/app/security/rewrite.py`
- Create: `backend/tests/security/test_row_level.py`

**Interfaces:**
- Consumes: `app.security.principal.Principal`/`RowRule`、`app.semantic.model.DatasetDef`、`app.compiler.predicates`
- Produces:
  - `app.security.rewrite.AppliedRowFilter` — frozen dataclass `field_business_name`/`values`（业务值，用于引证中的「由数据权限自动附加」）
  - `app.security.rewrite.inject_row_policies(ast, dataset, principal) -> tuple[exp.Expression, tuple[AppliedRowFilter, ...]]`

- [ ] **Step 1: 写失败的 RLS 测试**

`backend/tests/security/test_row_level.py`：

```python
from datetime import date

import pytest
import sqlglot

from app.compiler.query import compile_intent
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.principal import load_principal
from app.security.rewrite import inject_row_policies
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def _rewrite(session, username, **intent_overrides):
    dataset = load_dataset(session, "orders")
    compiled = compile_intent(dataset, _intent(**intent_overrides))
    principal = load_principal(session, username)
    ast, applied = inject_row_policies(compiled.ast, dataset, principal)
    return ast.sql(dialect="postgres"), applied


def test_row_policy_is_injected_into_where(env):
    sql, applied = _rewrite(env, "east_manager")

    assert "region_code" in sql
    assert "'EC'" in sql
    assert len(applied) == 1
    assert applied[0].field_business_name == "大区"
    assert applied[0].values == ("华东",)


def test_admin_query_is_untouched(env):
    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent())
    principal = load_principal(env, "admin")

    ast, applied = inject_row_policies(compiled.ast, dataset, principal)
    assert ast.sql(dialect="postgres") == compiled.sql_compact
    assert applied == ()


def test_policy_is_anded_not_ored_with_user_filters(env):
    from app.intent.schema import FilterCondition, FilterOperator

    sql, _ = _rewrite(
        env,
        "east_manager",
        filters=[
            FilterCondition(
                field="channel",
                operator=FilterOperator.EQ,
                values=["online"],
                spoken_values=["线上"],
            )
        ],
    )
    upper = sql.upper()
    assert " OR " not in upper
    assert upper.count(" AND ") >= 2


def test_user_cannot_widen_scope_by_filtering_another_region(env):
    """A user asking for South China while scoped to East gets an empty scope,
    never South China data."""
    from app.intent.schema import FilterCondition, FilterOperator

    sql, _ = _rewrite(
        env,
        "east_manager",
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ],
    )
    # Both predicates survive; their conjunction is unsatisfiable, which is correct.
    assert "'SC'" in sql and "'EC'" in sql
    assert " OR " not in sql.upper()


def test_multiple_roles_apply_all_policies(env):
    sql, applied = _rewrite(env, "multi_role")

    assert "region_code" in sql
    assert "channel" in sql
    assert {item.field_business_name for item in applied} == {"大区", "渠道"}


def test_policy_reaches_every_cte_of_a_comparison_query(env):
    sql, _ = _rewrite(env, "east_manager", comparison=ComparisonKind.MOM)

    # Both the current and the baseline CTE must carry the restriction;
    # patching only the outer query would leak the baseline period.
    assert sql.count("region_code") >= 2


def test_policy_values_are_physical_in_sql_and_business_in_citation(env):
    sql, applied = _rewrite(env, "east_manager")
    assert "华东" not in sql
    assert applied[0].values == ("华东",)


def test_unmapped_policy_value_stays_physical_in_citation(meta_session):
    """A policy value with no dictionary entry must still be shown, not dropped."""
    from app.security.orm import RoleRow, RowPolicyRow, UserRow

    build_orders_dataset(meta_session)
    role = RoleRow(
        name="odd",
        max_sensitivity="public",
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="region_code", operator="in", values=["ZZ"]
            )
        ],
    )
    meta_session.add(UserRow(username="odd_user", roles=[role]))
    meta_session.flush()

    sql, applied = _rewrite(meta_session, "odd_user")
    assert "'ZZ'" in sql
    assert applied[0].values == ("ZZ",)


def test_injected_sql_still_parses(env):
    sql, _ = _rewrite(env, "multi_role", comparison=ComparisonKind.YOY)
    assert sqlglot.parse_one(sql, dialect="postgres") is not None


def test_policy_on_field_absent_from_dataset_is_rejected(meta_session):
    from app.security.orm import RoleRow, RowPolicyRow, UserRow
    from app.security.rewrite import RowPolicyConfigError

    build_orders_dataset(meta_session)
    role = RoleRow(
        name="stale",
        max_sensitivity="public",
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="deleted_field", operator="in", values=["x"]
            )
        ],
    )
    meta_session.add(UserRow(username="stale_user", roles=[role]))
    meta_session.flush()

    # Fail closed: a stale policy must never silently widen access.
    with pytest.raises(RowPolicyConfigError):
        _rewrite(meta_session, "stale_user")
```

- [ ] **Step 2: 补齐 `CompiledQuery.sql_compact`**

上面的 `test_admin_query_is_untouched` 需要与改写后 SQL 同格式的基线。在计划 02 的 `app/compiler/query.py` 中为 `CompiledQuery` 增加一个字段：

```python
    sql_compact: str = ""
```

并在 `compile_intent` 的返回中填充：

```python
        sql=tree.sql(dialect="postgres", pretty=True),
        sql_compact=tree.sql(dialect="postgres"),
```

`sql` 用于展示给用户，`sql_compact` 用于比对与执行。

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_row_level.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security.rewrite'`

- [ ] **Step 4: 写 RLS 注入**

`backend/app/security/rewrite.py`：

```python
"""Security rewrites on the compiled AST.

Row policies are injected by walking every SELECT node in the tree, not just
the outermost one: a comparison query is two CTEs, and restricting only the
outer SELECT would leave the baseline period fully readable.
"""

from dataclasses import dataclass

from sqlglot import exp

from app.security.principal import Principal, RowRule
from app.semantic.model import DatasetDef


class RowPolicyConfigError(Exception):
    """A policy references something the dataset no longer has.

    Raised instead of skipping the policy: failing closed is the only safe
    behaviour when the alternative is silently granting wider access.
    """


@dataclass(frozen=True, slots=True)
class AppliedRowFilter:
    """What the citation block shows as 「由数据权限自动附加」."""

    field_business_name: str
    values: tuple[str, ...]


def _policy_predicate(dataset: DatasetDef, rule: RowRule) -> exp.Expression:
    if not dataset.has_field(rule.field_name):
        raise RowPolicyConfigError(
            f"行权限策略引用了数据集 {dataset.name} 中不存在的字段 {rule.field_name}"
        )
    field = dataset.field(rule.field_name)
    column = exp.column(field.physical_column)
    literals = [exp.Literal.string(value) for value in rule.values]

    if rule.operator == "not_in":
        return exp.Not(this=exp.In(this=column, expressions=literals))
    return exp.In(this=column, expressions=literals)


def _business_values(dataset: DatasetDef, rule: RowRule) -> tuple[str, ...]:
    """Physical policy values rendered for display.

    Values without a dictionary entry are shown as-is rather than dropped —
    an incomplete permission line is worse than an unpolished one.
    """
    field = dataset.field(rule.field_name)
    labels: list[str] = []
    for value in rule.values:
        match = next(
            (item for item in field.enum_values if item.physical_value == value),
            None,
        )
        labels.append(match.business_value if match else value)
    return tuple(labels)


def inject_row_policies(
    ast: exp.Expression, dataset: DatasetDef, principal: Principal
) -> tuple[exp.Expression, tuple[AppliedRowFilter, ...]]:
    rules = principal.row_rules_for(dataset.name)
    if not rules:
        return ast, ()

    predicates = [_policy_predicate(dataset, rule) for rule in rules]
    applied = tuple(
        AppliedRowFilter(
            field_business_name=dataset.field(rule.field_name).business_name or rule.field_name,
            values=_business_values(dataset, rule),
        )
        for rule in rules
    )

    rewritten = ast.copy()
    for select in rewritten.find_all(exp.Select):
        # Only SELECTs reading the physical table need the restriction; the
        # outer SELECT of a comparison query reads CTEs, whose sources are
        # already restricted.
        if not _reads_physical_table(select, dataset):
            continue
        for predicate in predicates:
            select.where(predicate.copy(), copy=False)

    return rewritten, applied


def _reads_physical_table(select: exp.Select, dataset: DatasetDef) -> bool:
    table_name = dataset.physical_table.split(".")[-1]
    return any(
        isinstance(source, exp.Table) and source.name == table_name
        for source in select.find_all(exp.Table)
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_row_level.py -v`
Expected: PASS（10 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/security/rewrite.py backend/tests/security/test_row_level.py backend/app/compiler/query.py
git commit -F - <<'EOF'
实现行级权限的 AST 注入

对比查询编译为双 CTE，若只给最外层 SELECT 加限制，对比期那一侧会被完整读到，等于权限失效。因此改写遍历树中所有读取物理表的 SELECT 节点逐一注入，并且与用户过滤条件以 AND 合并，使用户无法通过筛选其他区域扩大范围。

- 行策略注入到每个读取物理表的 SELECT，覆盖当期与对比期两侧
- 策略谓词与用户过滤条件取合集而非并集，越权筛选只会得到空集
- 策略引用了数据集中不存在的字段时直接失败，不跳过该策略
- 同时产出业务值形式的权限过滤描述，供引证块显示为自动附加
- 编译产物增加紧凑版 SQL，用于比对与执行
- 验证：pytest tests/security/test_row_level.py 10 项通过
EOF
```

---

### Task 3: 列权限与脱敏

**Files:**
- Create: `backend/app/security/columns.py`
- Create: `backend/tests/security/test_columns.py`

**Interfaces:**
- Consumes: `app.security.principal.Principal`/`ColumnAccess`、`app.semantic.model.DatasetDef`、`app.intent.schema.QueryIntent`
- Produces:
  - `app.security.columns.PermissionDeniedError` — 消息固定为「你没有该数据的访问权限」，不含任何元数据
  - `app.security.columns.visible_dataset(dataset, principal) -> DatasetDef` — 剔除 DENY 字段与引用它们的指标，用于召回阶段即不可见
  - `app.security.columns.assert_intent_permitted(dataset, intent, principal) -> None`
  - `app.security.columns.apply_masking(ast, dataset, principal) -> tuple[exp.Expression, tuple[str, ...]]` — 返回改写后的树与被脱敏字段的业务名

- [ ] **Step 1: 写失败的列权限测试**

`backend/tests/security/test_columns.py`：

```python
from datetime import date

import pytest

from app.intent.schema import (
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.columns import (
    PermissionDeniedError,
    apply_masking,
    assert_intent_permitted,
    visible_dataset,
)
from app.security.principal import load_principal
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_denied_field_is_absent_from_visible_dataset(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "east_manager"))

    assert not visible.has_field("cost")
    assert visible.has_field("amount")


def test_metrics_depending_on_denied_fields_are_removed(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "east_manager"))
    names = {metric.name for metric in visible.metrics}

    # total_cost reads cost directly; gross_margin_rate is computed from it.
    assert "total_cost" not in names
    assert "gross_margin_rate" not in names
    assert "sales_revenue" in names


def test_admin_sees_everything(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "admin"))

    assert len(visible.fields) == len(dataset.fields)
    assert len(visible.metrics) == len(dataset.metrics)


def test_masked_field_stays_visible_in_the_model(env):
    dataset = load_dataset(env, "orders")
    # analyst is masked on customer_name, not denied: it is still queryable.
    visible = visible_dataset(dataset, load_principal(env, "analyst"))
    assert visible.has_field("customer_name")


def test_querying_a_denied_metric_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, _intent(metrics=["total_cost"]), principal)


def test_grouping_by_a_denied_field_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, _intent(dimensions=["cost"]), principal)


def test_filtering_on_a_denied_field_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")
    intent = _intent(
        filters=[
            FilterCondition(
                field="cost", operator=FilterOperator.GT, values=["100"], spoken_values=["一百"]
            )
        ]
    )

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, intent, principal)


def test_permission_error_leaks_no_metadata(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_intent_permitted(dataset, _intent(metrics=["total_cost"]), principal)

    message = str(excinfo.value)
    for leak in ("cost", "total_cost", "orders", "sample", "region_code"):
        assert leak not in message


def test_filtering_on_a_masked_field_is_refused(env):
    """Masking hides the value; filtering on it would reveal it by inference."""
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "analyst")
    intent = _intent(
        filters=[
            FilterCondition(
                field="customer_name",
                operator=FilterOperator.EQ,
                values=["ACME"],
                spoken_values=["ACME"],
            )
        ]
    )

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, intent, principal)


def test_permitted_intent_passes_silently(env):
    dataset = load_dataset(env, "orders")
    assert assert_intent_permitted(dataset, _intent(), load_principal(env, "admin")) is None


def test_masking_rewrites_the_projection(env):
    from app.compiler.query import compile_intent

    dataset = load_dataset(env, "orders")
    intent = _intent(kind=IntentKind.DETAIL, dimensions=["customer_name", "province"])
    compiled = compile_intent(dataset, intent)

    ast, masked = apply_masking(compiled.ast, dataset, load_principal(env, "analyst"))
    sql = ast.sql(dialect="postgres")

    assert "***" in sql
    assert masked == ("客户名称",)
    # The alias must survive so downstream column mapping still works.
    assert "customer_name" in sql


def test_masking_is_a_noop_for_permitted_columns(env):
    from app.compiler.query import compile_intent

    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent(dimensions=["province"]))

    ast, masked = apply_masking(compiled.ast, dataset, load_principal(env, "analyst"))
    assert masked == ()
    assert ast.sql(dialect="postgres") == compiled.sql_compact
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_columns.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security.columns'`

- [ ] **Step 3: 写列权限与脱敏**

`backend/app/security/columns.py`：

```python
"""Column permissions and masking.

Two distinct mechanisms:

- DENY removes the field from the semantic view entirely, so it is invisible at
  recall time — the model is never told a forbidden field exists.
- MASK keeps the field queryable but replaces its value in the projection.

Filtering on a masked field is refused: the value would be recoverable by
probing which filters return rows.
"""

from dataclasses import replace

from sqlglot import exp

from app.security.principal import ColumnAccess, Principal
from app.intent.schema import QueryIntent
from app.semantic.model import DatasetDef, MetricDef

_MASK_LITERAL = "***"


class PermissionDeniedError(Exception):
    """Fixed message: revealing which object was denied confirms it exists."""

    def __init__(self) -> None:
        super().__init__("你没有该数据的访问权限")


def _access(dataset: DatasetDef, principal: Principal, field_name: str) -> ColumnAccess:
    if not dataset.has_field(field_name):
        return ColumnAccess.ALLOW
    return principal.column_access(dataset.field(field_name), dataset.name)


def _metric_field_names(dataset: DatasetDef, metric: MetricDef, seen: set[str]) -> set[str]:
    """Fields a metric ultimately reads, following metric references."""
    if metric.name in seen:
        return set()
    seen.add(metric.name)

    names: set[str] = set()
    if metric.source_field:
        names.add(metric.source_field)
    if metric.expression:
        for token in metric.expression.replace("(", " ").replace(")", " ").split():
            if dataset.has_metric(token):
                names |= _metric_field_names(dataset, dataset.metric(token), seen)
            elif dataset.has_field(token):
                names.add(token)
    return names


def visible_dataset(dataset: DatasetDef, principal: Principal) -> DatasetDef:
    denied = {
        field.name
        for field in dataset.fields
        if principal.column_access(field, dataset.name) == ColumnAccess.DENY
    }
    if not denied:
        return dataset

    fields = tuple(field for field in dataset.fields if field.name not in denied)
    metrics = tuple(
        metric
        for metric in dataset.metrics
        if not (_metric_field_names(dataset, metric, set()) & denied)
    )
    return replace(dataset, fields=fields, metrics=metrics)


def assert_intent_permitted(
    dataset: DatasetDef, intent: QueryIntent, principal: Principal
) -> None:
    for metric_name in intent.metrics:
        if not dataset.has_metric(metric_name):
            continue
        metric = dataset.metric(metric_name)
        for field_name in _metric_field_names(dataset, metric, set()):
            if _access(dataset, principal, field_name) == ColumnAccess.DENY:
                raise PermissionDeniedError

    for name in intent.dimensions:
        if _access(dataset, principal, name) == ColumnAccess.DENY:
            raise PermissionDeniedError

    for condition in intent.filters:
        # MASK also blocks filtering: a permitted filter would leak the value.
        if _access(dataset, principal, condition.field) != ColumnAccess.ALLOW:
            raise PermissionDeniedError


def apply_masking(
    ast: exp.Expression, dataset: DatasetDef, principal: Principal
) -> tuple[exp.Expression, tuple[str, ...]]:
    masked_columns = {
        field.physical_column: field
        for field in dataset.fields
        if principal.column_access(field, dataset.name) == ColumnAccess.MASK
    }
    if not masked_columns:
        return ast, ()

    hit: dict[str, str] = {}
    rewritten = ast.copy()

    for alias in rewritten.find_all(exp.Alias):
        inner = alias.this
        if isinstance(inner, exp.Column) and inner.name in masked_columns:
            field = masked_columns[inner.name]
            hit[field.name] = field.business_name or field.name
            # Keep the alias so downstream column mapping is unaffected.
            alias.set("this", exp.Literal.string(_MASK_LITERAL))

    return rewritten, tuple(hit[name] for name in sorted(hit))
```

- [ ] **Step 4: 补齐 `DatasetDef.has_metric`**

计划 01 的 `DatasetDef` 只有 `has_field`。在 `app/semantic/model.py` 中补一个对称方法：

```python
    def has_metric(self, name: str) -> bool:
        return any(item.name == name for item in self.metrics)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_columns.py -v`
Expected: PASS（12 项）

- [ ] **Step 6: 提交**

```bash
git add backend/app/security/columns.py backend/tests/security/test_columns.py backend/app/semantic/model.py
git commit -F - <<'EOF'
实现列权限剔除与敏感字段脱敏

无权列不能只在结果里去掉：若字段仍留在语义视图中，模型会把它写进意图，用户从澄清选项里就能推断该字段存在。因此拒绝级别的字段在召回阶段即从语义视图剔除，连引用它的指标一并移除。

- 无权字段与依赖它的指标从语义视图剔除，模型无从得知其存在
- 脱敏字段保留可查询性，投影替换为掩码字面量并保留别名
- 禁止对脱敏字段做过滤，避免通过试探筛选反推真实值
- 越权错误统一为固定文案，测试断言其中不含任何表名与字段名
- 指标依赖解析递归展开指标间引用，复合指标的底层字段同样受控
- 验证：pytest tests/security/test_columns.py 12 项通过
EOF
```

---

### Task 4: AST 白名单终检

**Files:**
- Create: `backend/app/security/whitelist.py`
- Create: `backend/tests/security/test_whitelist.py`

**Interfaces:**
- Consumes: sqlglot
- Produces:
  - `app.security.whitelist.AstRejectedError` — 属性 `reason: str`（面向管理员，不返回给终端用户）
  - `app.security.whitelist.assert_select_only(ast) -> None`
  - `app.security.whitelist.assert_within_dataset(ast, allowed_tables) -> None`
  - `app.security.whitelist.enforce_limit(ast, max_rows) -> exp.Expression`

- [ ] **Step 1: 写失败的白名单测试**

`backend/tests/security/test_whitelist.py`：

```python
import pytest
import sqlglot

from app.security.whitelist import (
    AstRejectedError,
    assert_select_only,
    assert_within_dataset,
    enforce_limit,
)


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE sample.orders",
        "DELETE FROM sample.orders",
        "UPDATE sample.orders SET amount = 0",
        "INSERT INTO sample.orders (amount) VALUES (1)",
        "TRUNCATE sample.orders",
        "ALTER TABLE sample.orders ADD COLUMN x int",
        "CREATE TABLE t AS SELECT 1",
        "GRANT SELECT ON sample.orders TO public",
        "COPY sample.orders TO '/tmp/x.csv'",
    ],
)
def test_ddl_and_dml_are_rejected(sql):
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse(sql))


def test_plain_select_is_accepted():
    assert assert_select_only(_parse("SELECT SUM(amount) FROM sample.orders")) is None


def test_cte_select_is_accepted():
    sql = "WITH a AS (SELECT amount FROM sample.orders) SELECT SUM(amount) FROM a"
    assert assert_select_only(_parse(sql)) is None


def test_multiple_statements_are_rejected():
    # A stacked statement is the classic injection shape.
    statements = sqlglot.parse("SELECT 1; DROP TABLE sample.orders", dialect="postgres")
    with pytest.raises(AstRejectedError):
        for statement in statements:
            assert_select_only(statement)


def test_select_into_is_rejected():
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse("SELECT * INTO backup FROM sample.orders"))


def test_locking_clause_is_rejected():
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse("SELECT amount FROM sample.orders FOR UPDATE"))


def test_query_outside_allowed_tables_is_rejected():
    ast = _parse("SELECT * FROM finance.revenue")
    with pytest.raises(AstRejectedError):
        assert_within_dataset(ast, {"sample.orders"})


def test_query_on_allowed_table_passes():
    ast = _parse("SELECT SUM(amount) FROM sample.orders")
    assert assert_within_dataset(ast, {"sample.orders"}) is None


def test_cte_names_are_not_mistaken_for_tables():
    sql = "WITH current_period AS (SELECT amount FROM sample.orders) SELECT * FROM current_period"
    assert assert_within_dataset(_parse(sql), {"sample.orders"}) is None


def test_union_reaching_another_table_is_rejected():
    sql = "SELECT amount FROM sample.orders UNION ALL SELECT amount FROM finance.revenue"
    with pytest.raises(AstRejectedError):
        assert_within_dataset(_parse(sql), {"sample.orders"})


def test_subquery_reaching_another_table_is_rejected():
    sql = "SELECT * FROM sample.orders WHERE amount > (SELECT MAX(amount) FROM finance.revenue)"
    with pytest.raises(AstRejectedError):
        assert_within_dataset(_parse(sql), {"sample.orders"})


def test_limit_is_added_when_absent():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders"), 1000)
    assert "LIMIT 1000" in ast.sql(dialect="postgres").upper()


def test_existing_smaller_limit_is_kept():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders LIMIT 10"), 1000)
    assert "LIMIT 10" in ast.sql(dialect="postgres").upper()


def test_oversized_limit_is_clamped():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders LIMIT 999999"), 1000)
    upper = ast.sql(dialect="postgres").upper()
    assert "LIMIT 1000" in upper
    assert "999999" not in upper


def test_rejection_reason_is_admin_facing():
    with pytest.raises(AstRejectedError) as excinfo:
        assert_select_only(_parse("DROP TABLE sample.orders"))
    assert excinfo.value.reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_whitelist.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security.whitelist'`

- [ ] **Step 3: 写白名单**

`backend/app/security/whitelist.py`：

```python
"""The final gate before execution (spec M-10).

A whitelist, not a blacklist: the statement must be a SELECT (optionally with
CTEs) and every table it touches must be explicitly allowed. Anything the
whitelist does not recognise is rejected rather than assumed harmless.

This runs on the tree that will actually execute — after rewriting — so that
problems introduced by the rewrites themselves are caught too.
"""

from sqlglot import exp

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Subquery)

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
    exp.Command,
    exp.Into,
)


class AstRejectedError(Exception):
    """Structural rejection. The reason is for administrators and Trace."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def assert_select_only(ast: exp.Expression) -> None:
    if not isinstance(ast, _ALLOWED_ROOTS):
        raise AstRejectedError(f"仅允许 SELECT 语句，实际为 {type(ast).__name__}")

    for node_type in _FORBIDDEN_NODES:
        node = ast.find(node_type)
        if node is not None:
            raise AstRejectedError(f"语句中包含被禁止的节点 {type(node).__name__}")

    if ast.args.get("locks"):
        raise AstRejectedError("禁止使用行锁子句")


def _cte_names(ast: exp.Expression) -> set[str]:
    return {cte.alias_or_name for cte in ast.find_all(exp.CTE)}


def assert_within_dataset(ast: exp.Expression, allowed_tables: set[str]) -> None:
    """Every physical table read must be in the allow list.

    CTE references are skipped: they resolve to definitions in the same tree,
    which are themselves checked.
    """
    known_ctes = _cte_names(ast)
    normalized = {name.lower() for name in allowed_tables}

    for table in ast.find_all(exp.Table):
        if table.name in known_ctes and not table.db:
            continue
        qualified = f"{table.db}.{table.name}" if table.db else table.name
        if qualified.lower() not in normalized:
            raise AstRejectedError(f"查询访问了未授权的表 {qualified}")


def enforce_limit(ast: exp.Expression, max_rows: int) -> exp.Expression:
    """Guarantee an upper bound on returned rows, clamping anything larger."""
    limited = ast.copy()
    existing = limited.args.get("limit")

    if existing is not None:
        try:
            current = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            current = max_rows + 1
        if current <= max_rows:
            return limited

    return limited.limit(max_rows)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_whitelist.py -v`
Expected: PASS（23 项，其中 DDL/DML 用例为 9 个参数化实例）

若某个被禁语句在当前 sqlglot 版本下解析为 `exp.Command`（版本间会有差异），它已被 `_FORBIDDEN_NODES` 覆盖；如出现解析异常，在 `assert_select_only` 入口前把 `sqlglot.ParseError` 也视为拒绝，不要放宽用例。

- [ ] **Step 5: 提交**

```bash
git add backend/app/security/whitelist.py backend/tests/security/test_whitelist.py
git commit -F - <<'EOF'
实现执行前的 AST 白名单终检

黑名单式校验永远漏得掉新的写入语法，因此改为白名单：根节点必须是查询，触达的每张物理表都要在授权集合内。终检放在全部改写之后，检查真正要执行的那棵树，使改写本身引入的问题也能被拦下。

- 仅允许查询类根节点，写入、建表、授权、导出等节点一律拒绝
- 逐一校验触达的物理表，子查询与 UNION 中的越界表同样拦截
- CTE 名不误判为物理表，其定义体照常受检
- 强制行数上限，已有更小的限制保留，超限的限制被收紧
- 拒绝原因面向管理员并写入 Trace，不返回终端用户
- 验证：pytest tests/security/test_whitelist.py 23 项通过
EOF
```

---

### Task 5: 成本护栏

**Files:**
- Create: `backend/app/security/guardrails.py`
- Create: `backend/tests/security/test_guardrails.py`

**Interfaces:**
- Consumes: `app.core.config.Settings`、`app.core.db.sample_engine`
- Produces:
  - `app.security.guardrails.CostVerdict` — 枚举 `PASS`/`WARN`/`REJECT`
  - `app.security.guardrails.CostEstimate` — frozen dataclass `verdict`/`estimated_rows`/`estimated_cost`/`message`
  - `app.security.guardrails.QueryTooExpensiveError` — 属性 `estimate: CostEstimate`
  - `app.security.guardrails.estimate_cost(connection, sql, settings) -> CostEstimate`
  - `app.security.guardrails.assert_affordable(connection, sql, settings) -> CostEstimate` — `REJECT` 时抛异常

- [ ] **Step 1: 写失败的护栏测试**

`backend/tests/security/test_guardrails.py`：

```python
import pytest

from app.core.config import Settings
from app.security.guardrails import (
    CostVerdict,
    QueryTooExpensiveError,
    assert_affordable,
    estimate_cost,
)


def _settings(**overrides) -> Settings:
    base = {"cost_warn_rows": 100, "cost_reject_rows": 1000}
    base.update(overrides)
    return Settings(**base)


def test_small_query_passes(sample_conn):
    estimate = estimate_cost(sample_conn, "SELECT * FROM sample.orders", _settings())

    assert estimate.verdict == CostVerdict.PASS
    assert estimate.estimated_rows > 0
    assert estimate.estimated_cost > 0


def test_estimate_does_not_execute_the_query(sample_conn):
    """EXPLAIN without ANALYZE must not touch rows.

    The division is written against columns rather than constants so the planner
    cannot fold it: only actual execution would divide by zero.
    """
    estimate = estimate_cost(
        sample_conn,
        "SELECT amount / (quantity - quantity) FROM sample.orders",
        _settings(),
    )
    assert estimate.verdict in (CostVerdict.PASS, CostVerdict.WARN)


def test_query_over_warn_threshold_warns(sample_conn):
    estimate = estimate_cost(
        sample_conn, "SELECT * FROM sample.orders", _settings(cost_warn_rows=1)
    )

    assert estimate.verdict == CostVerdict.WARN
    assert estimate.message


def test_query_over_reject_threshold_is_rejected(sample_conn):
    estimate = estimate_cost(
        sample_conn,
        "SELECT * FROM sample.orders",
        _settings(cost_warn_rows=1, cost_reject_rows=2),
    )
    assert estimate.verdict == CostVerdict.REJECT


def test_assert_affordable_raises_on_reject(sample_conn):
    settings = _settings(cost_warn_rows=1, cost_reject_rows=2)

    with pytest.raises(QueryTooExpensiveError) as excinfo:
        assert_affordable(sample_conn, "SELECT * FROM sample.orders", settings)

    assert excinfo.value.estimate.verdict == CostVerdict.REJECT


def test_assert_affordable_returns_estimate_on_warn(sample_conn):
    estimate = assert_affordable(
        sample_conn, "SELECT * FROM sample.orders", _settings(cost_warn_rows=1)
    )
    assert estimate.verdict == CostVerdict.WARN


def test_aggregate_query_estimates_one_row(sample_conn):
    estimate = estimate_cost(
        sample_conn, "SELECT SUM(amount) FROM sample.orders", _settings()
    )
    assert estimate.estimated_rows == 1


def test_unparseable_plan_fails_closed(sample_conn):
    """If the plan cannot be read, reject rather than assume it is cheap."""
    with pytest.raises(QueryTooExpensiveError):
        assert_affordable(sample_conn, "SELECT * FROM sample.no_such_table", _settings())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_guardrails.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security.guardrails'`

- [ ] **Step 3: 写成本护栏**

`backend/app/security/guardrails.py`：

```python
"""Cost guardrails (spec M-13).

Uses PostgreSQL's EXPLAIN (FORMAT JSON) without ANALYZE: the planner's row and
cost estimates are read without executing anything. Any failure to obtain a
plan is treated as REJECT — assuming an unreadable query is cheap is how a
guardrail becomes decorative.
"""

import json
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings


class CostVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    verdict: CostVerdict
    estimated_rows: int
    estimated_cost: float
    message: str = ""


class QueryTooExpensiveError(Exception):
    def __init__(self, estimate: CostEstimate) -> None:
        self.estimate = estimate
        super().__init__(estimate.message)


def estimate_cost(connection: Connection, sql: str, settings: Settings) -> CostEstimate:
    try:
        raw = connection.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar_one()
    except SQLAlchemyError as error:
        return CostEstimate(
            verdict=CostVerdict.REJECT,
            estimated_rows=0,
            estimated_cost=0.0,
            message=f"无法获取查询计划，按拒绝处理：{error.__class__.__name__}",
        )

    plan = json.loads(raw) if isinstance(raw, str) else raw
    try:
        root = plan[0]["Plan"]
        rows = int(root["Plan Rows"])
        cost = float(root["Total Cost"])
    except (KeyError, IndexError, TypeError, ValueError):
        return CostEstimate(
            verdict=CostVerdict.REJECT,
            estimated_rows=0,
            estimated_cost=0.0,
            message="查询计划结构无法解析，按拒绝处理",
        )

    if rows >= settings.cost_reject_rows:
        return CostEstimate(
            CostVerdict.REJECT,
            rows,
            cost,
            f"预估扫描 {rows} 行，超过上限 {settings.cost_reject_rows} 行，已拒绝执行",
        )
    if rows >= settings.cost_warn_rows:
        return CostEstimate(
            CostVerdict.WARN,
            rows,
            cost,
            f"预估扫描 {rows} 行，数据量较大，建议缩小时间范围",
        )
    return CostEstimate(CostVerdict.PASS, rows, cost)


def assert_affordable(connection: Connection, sql: str, settings: Settings) -> CostEstimate:
    estimate = estimate_cost(connection, sql, settings)
    if estimate.verdict == CostVerdict.REJECT:
        raise QueryTooExpensiveError(estimate)
    return estimate
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_guardrails.py -v`
Expected: PASS（8 项）

样本表只有 14 行，因此测试用低阈值（`cost_warn_rows=1`）触发 WARN 与 REJECT，而非造大表。

- [ ] **Step 5: 提交**

```bash
git add backend/app/security/guardrails.py backend/tests/security/test_guardrails.py
git commit -F - <<'EOF'
实现基于查询计划的成本护栏

护栏若在取不到计划时放行，等于形同虚设，而这恰是最容易出现的情况（表名错误、权限不足、语法边界）。因此实现上把任何无法获得或无法解析计划的情形都判为拒绝。

- 用不带 ANALYZE 的 EXPLAIN 取行数与总成本，不实际执行查询
- 分警告与拒绝两档阈值，警告档返回可读提示供答案展示
- 取不到计划或计划结构异常时判为拒绝而非放行
- 验证：pytest tests/security/test_guardrails.py 8 项通过
EOF
```

---

### Task 6: 安全改写流水线

**Files:**
- Create: `backend/app/security/pipeline.py`
- Create: `backend/tests/security/test_pipeline.py`

**Interfaces:**
- Consumes: Task 2~5 的全部导出、`app.compiler.query.CompiledQuery`、`app.core.config.Settings`
- Produces:
  - `app.security.pipeline.SecuredQuery` — frozen dataclass `sql`/`display_sql`/`ast`/`applied_row_filters`/`masked_field_names`/`cost`/`row_limit`
  - `app.security.pipeline.secure_compiled(compiled, dataset, principal, connection, settings) -> SecuredQuery`
  - `app.security.pipeline.secure_verified_sql(sql, dataset, principal, connection, settings) -> SecuredQuery` — Verified Query 的固定 SQL 走同一条流水线

- [ ] **Step 1: 写失败的流水线测试**

`backend/tests/security/test_pipeline.py`：

```python
from datetime import date

import pytest

from app.compiler.query import compile_intent
from app.core.config import Settings
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.pipeline import secure_compiled, secure_verified_sql
from app.security.principal import load_principal
from app.security.whitelist import AstRejectedError
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _settings(**overrides) -> Settings:
    base = {"max_result_rows": 1000, "cost_warn_rows": 10_000, "cost_reject_rows": 100_000}
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def _secure(session, sample_conn, username, **intent_overrides):
    dataset = load_dataset(session, "orders")
    compiled = compile_intent(dataset, _intent(**intent_overrides))
    principal = load_principal(session, username)
    return secure_compiled(compiled, dataset, principal, sample_conn, _settings())


def test_pipeline_applies_row_policy_and_limit(env, sample_conn):
    secured = _secure(env, sample_conn, "east_manager")

    assert "'EC'" in secured.sql
    assert "LIMIT 1000" in secured.sql.upper()
    assert secured.row_limit == 1000
    assert secured.applied_row_filters[0].field_business_name == "大区"


def test_pipeline_reports_masked_fields(env, sample_conn):
    secured = _secure(
        env, sample_conn, "analyst", kind=IntentKind.DETAIL, dimensions=["customer_name"]
    )
    assert secured.masked_field_names == ("客户名称",)
    assert "***" in secured.sql


def test_pipeline_output_is_executable(env, sample_conn):
    from sqlalchemy import text

    secured = _secure(env, sample_conn, "east_manager", dimensions=["province"])
    rows = sample_conn.execute(text(secured.sql)).fetchall()
    assert rows is not None


def test_comparison_query_survives_the_full_pipeline(env, sample_conn):
    from sqlalchemy import text

    secured = _secure(env, sample_conn, "east_manager", comparison=ComparisonKind.MOM)
    assert secured.sql.count("region_code") >= 2
    sample_conn.execute(text(secured.sql)).fetchall()


def test_cost_estimate_is_attached(env, sample_conn):
    secured = _secure(env, sample_conn, "admin")
    assert secured.cost.estimated_rows >= 0


def test_expensive_query_is_rejected_by_pipeline(env, sample_conn):
    from app.security.guardrails import QueryTooExpensiveError

    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent())
    principal = load_principal(env, "admin")

    with pytest.raises(QueryTooExpensiveError):
        secure_compiled(
            compiled,
            dataset,
            principal,
            sample_conn,
            _settings(cost_warn_rows=1, cost_reject_rows=1),
        )


def test_verified_sql_also_gets_row_policy(env, sample_conn):
    """The whole point of routing Verified Query through the same pipeline."""
    secured = secure_verified_sql(
        "SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        load_dataset(env, "orders"),
        load_principal(env, "east_manager"),
        sample_conn,
        _settings(),
    )

    assert "region_code" in secured.sql
    assert "'EC'" in secured.sql


def test_verified_sql_touching_another_table_is_rejected(env, sample_conn):
    with pytest.raises(AstRejectedError):
        secure_verified_sql(
            "SELECT 1 FROM finance.revenue",
            load_dataset(env, "orders"),
            load_principal(env, "admin"),
            sample_conn,
            _settings(),
        )


def test_verified_sql_with_dml_is_rejected(env, sample_conn):
    with pytest.raises(AstRejectedError):
        secure_verified_sql(
            "DELETE FROM sample.orders",
            load_dataset(env, "orders"),
            load_principal(env, "admin"),
            sample_conn,
            _settings(),
        )


def test_display_sql_is_pretty_and_matches_executed_sql(env, sample_conn):
    import sqlglot

    secured = _secure(env, sample_conn, "east_manager")
    left = sqlglot.parse_one(secured.sql, dialect="postgres")
    right = sqlglot.parse_one(secured.display_sql, dialect="postgres")

    assert "\n" in secured.display_sql
    # What the user is shown must be the statement that ran.
    assert left.sql(dialect="postgres") == right.sql(dialect="postgres")


def test_whitelist_runs_after_rewrites(env, sample_conn):
    """Order matters: the checked tree must be the one that executes."""
    secured = _secure(env, sample_conn, "east_manager")
    assert "'EC'" in secured.sql
    assert "LIMIT" in secured.sql.upper()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/security/test_pipeline.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.security.pipeline'`

- [ ] **Step 3: 写流水线**

`backend/app/security/pipeline.py`：

```python
"""Stage 5 of the pipeline: security rewrites and guardrails.

Fixed order, and the order is the point:

1. row policies   — inject before anything else reads the shape of the query
2. masking        — rewrite projections
3. forced limit   — bound the result set
4. AST whitelist  — check the tree that will actually execute
5. cost estimate  — plan the final statement, not an earlier draft

Verified Query SQL enters at the same door (spec 3.2): a stored statement that
skipped this would be a standing privilege-escalation path.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlalchemy.engine import Connection

from app.compiler.query import CompiledQuery
from app.core.config import Settings
from app.security.columns import apply_masking
from app.security.guardrails import CostEstimate, assert_affordable
from app.security.principal import Principal
from app.security.rewrite import AppliedRowFilter, inject_row_policies
from app.security.whitelist import (
    AstRejectedError,
    assert_select_only,
    assert_within_dataset,
    enforce_limit,
)
from app.semantic.model import DatasetDef


@dataclass(frozen=True, slots=True)
class SecuredQuery:
    sql: str
    display_sql: str
    ast: exp.Expression
    applied_row_filters: tuple[AppliedRowFilter, ...]
    masked_field_names: tuple[str, ...]
    cost: CostEstimate
    row_limit: int


def _run(
    ast: exp.Expression,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    ast, applied = inject_row_policies(ast, dataset, principal)
    ast, masked = apply_masking(ast, dataset, principal)
    ast = enforce_limit(ast, settings.max_result_rows)

    assert_select_only(ast)
    assert_within_dataset(ast, {dataset.physical_table})

    sql = ast.sql(dialect="postgres")
    cost = assert_affordable(connection, sql, settings)

    return SecuredQuery(
        sql=sql,
        display_sql=ast.sql(dialect="postgres", pretty=True),
        ast=ast,
        applied_row_filters=applied,
        masked_field_names=masked,
        cost=cost,
        row_limit=settings.max_result_rows,
    )


def secure_compiled(
    compiled: CompiledQuery,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    return _run(compiled.ast, dataset, principal, connection, settings)


def secure_verified_sql(
    sql: str,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except sqlglot.ParseError as error:
        raise AstRejectedError(f"固定 SQL 无法解析：{error}") from error
    if ast is None:
        raise AstRejectedError("固定 SQL 为空")

    return _run(ast, dataset, principal, connection, settings)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/security/test_pipeline.py -v`
Expected: PASS（11 项）

- [ ] **Step 5: 提交**

```bash
git add backend/app/security/pipeline.py backend/tests/security/test_pipeline.py
git commit -F - <<'EOF'
串联安全改写流水线并让固定 SQL 走同一入口

改写与校验的先后顺序决定了安全性是否成立：白名单必须检查真正要执行的那棵树，成本预估也必须针对最终语句。同时 Verified Query 的固定 SQL 若有独立执行路径，就成了绕过行权限的常设通道，因此让它从同一个入口进入。

- 固定顺序执行行权限注入、脱敏、强制限制、白名单终检、成本预估
- Verified Query 的固定 SQL 复用同一条流水线，解析失败即拒绝
- 展示用 SQL 与执行 SQL 出自同一棵树，避免用户看到的与实际运行的不一致
- 输出携带权限过滤描述、脱敏字段与成本预估，供答案与 Trace 使用
- 验证：pytest tests/security/test_pipeline.py 11 项通过
EOF
```

---

### Task 7: 执行与结果校验

**Files:**
- Create: `backend/app/execution/__init__.py`
- Create: `backend/app/execution/runner.py`
- Create: `backend/app/execution/validation.py`
- Create: `backend/tests/execution/__init__.py`
- Create: `backend/tests/execution/test_runner.py`
- Create: `backend/tests/execution/test_validation.py`

**Interfaces:**
- Consumes: `app.security.pipeline.SecuredQuery`、`app.core.db.sample_engine`、`app.core.config.Settings`
- Produces:
  - `app.execution.runner.QueryResult` — frozen dataclass `columns`/`rows`/`row_count`/`truncated`/`elapsed_ms`
  - `app.execution.runner.ExecutionFailedError` — 属性 `kind`（`timeout`/`connection`/`sql`/`unknown`）/`detail`
  - `app.execution.runner.execute(secured, settings) -> QueryResult` — 超时与连接失败重试 2 次，其余错误直接归类上报
  - `app.execution.validation.ValidationCode` — 枚举 `EMPTY_RESULT`/`ALL_NULL`/`FILTER_TOO_NARROW`/`ROW_COUNT_TRUNCATED`/`MAGNITUDE_SHIFT`
  - `app.execution.validation.ValidationIssue` — frozen dataclass `code`/`severity`(`block`/`warn`)/`message`
  - `app.execution.validation.validate_result(result, *, has_filters, comparison_columns) -> tuple[ValidationIssue, ...]`

- [ ] **Step 1: 写失败的执行测试**

`backend/tests/execution/test_runner.py`：

```python
import pytest

from app.core.config import Settings
from app.execution.runner import ExecutionFailedError, execute


def _settings(**overrides) -> Settings:
    base = {"max_result_rows": 1000, "execution_retry_attempts": 2}
    base.update(overrides)
    return Settings(**base)


class _FakeSecured:
    """Minimal stand-in: the runner only needs sql and row_limit."""

    def __init__(self, sql: str, row_limit: int = 1000) -> None:
        self.sql = sql
        self.row_limit = row_limit


def test_execute_returns_columns_and_rows(sample_conn):
    result = execute(
        _FakeSecured("SELECT region_code, SUM(amount) AS total FROM sample.orders GROUP BY 1"),
        _settings(),
        connection=sample_conn,
    )

    assert result.columns == ("region_code", "total")
    assert result.row_count == len(result.rows) > 0
    assert result.elapsed_ms >= 0


def test_execute_marks_truncation_at_the_limit(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders LIMIT 3", row_limit=3),
        _settings(),
        connection=sample_conn,
    )
    assert result.truncated is True


def test_execute_does_not_mark_truncation_below_the_limit(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders LIMIT 3", row_limit=100),
        _settings(),
        connection=sample_conn,
    )
    assert result.truncated is False


def test_empty_result_is_not_an_error(sample_conn):
    result = execute(
        _FakeSecured("SELECT * FROM sample.orders WHERE region_code = 'ZZ'"),
        _settings(),
        connection=sample_conn,
    )

    assert result.row_count == 0
    assert result.columns


def test_sql_error_is_classified_and_not_retried(sample_conn, monkeypatch):
    attempts = {"count": 0}
    original = sample_conn.execute

    def counting_execute(*args, **kwargs):
        attempts["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(sample_conn, "execute", counting_execute)

    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT no_such_column FROM sample.orders"),
            _settings(),
            connection=sample_conn,
        )

    assert excinfo.value.kind == "sql"
    # Retrying a broken statement only wastes time; it will fail identically.
    assert attempts["count"] == 1


def test_timeout_is_retried_then_reported(monkeypatch):
    from sqlalchemy.exc import OperationalError

    calls = {"count": 0}

    class _FlakyConnection:
        def execute(self, *args, **kwargs):
            calls["count"] += 1
            raise OperationalError("SELECT 1", {}, Exception("canceling statement due to timeout"))

    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT 1"),
            _settings(execution_retry_attempts=2),
            connection=_FlakyConnection(),
        )

    assert excinfo.value.kind == "timeout"
    # One initial attempt plus two retries.
    assert calls["count"] == 3


def test_transient_failure_then_success_returns_data(monkeypatch, sample_conn):
    from sqlalchemy.exc import OperationalError

    calls = {"count": 0}
    original = sample_conn.execute

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return original(*args, **kwargs)

    monkeypatch.setattr(sample_conn, "execute", flaky)

    result = execute(
        _FakeSecured("SELECT COUNT(*) AS n FROM sample.orders"),
        _settings(),
        connection=sample_conn,
    )

    assert calls["count"] == 2
    assert result.row_count == 1


def test_execution_error_detail_is_admin_facing(sample_conn):
    with pytest.raises(ExecutionFailedError) as excinfo:
        execute(
            _FakeSecured("SELECT no_such_column FROM sample.orders"),
            _settings(),
            connection=sample_conn,
        )
    assert excinfo.value.detail
```

- [ ] **Step 2: 写失败的结果校验测试**

`backend/tests/execution/test_validation.py`：

```python
from app.execution.runner import QueryResult
from app.execution.validation import ValidationCode, validate_result


def _result(columns, rows, *, truncated=False) -> QueryResult:
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=1,
    )


def _codes(issues):
    return {issue.code for issue in issues}


def test_normal_result_has_no_issues():
    issues = validate_result(_result(["region", "total"], [("EC", 100)]), has_filters=False)
    assert issues == ()


def test_empty_result_without_filters_is_reported_as_no_data():
    issues = validate_result(_result(["total"], []), has_filters=False)

    assert ValidationCode.EMPTY_RESULT in _codes(issues)
    assert ValidationCode.FILTER_TOO_NARROW not in _codes(issues)


def test_empty_result_with_filters_points_at_the_filters():
    issues = validate_result(_result(["total"], []), has_filters=True)

    # The distinction matters: "no data" and "your filters removed it" need
    # different follow-ups.
    assert ValidationCode.FILTER_TOO_NARROW in _codes(issues)


def test_empty_result_is_blocking():
    issues = validate_result(_result(["total"], []), has_filters=False)
    assert all(issue.severity == "block" for issue in issues)


def test_all_null_metric_is_blocking():
    issues = validate_result(_result(["region", "total"], [("EC", None), ("SC", None)]),
                             has_filters=False)

    assert ValidationCode.ALL_NULL in _codes(issues)
    assert all(issue.severity == "block" for issue in issues if issue.code == ValidationCode.ALL_NULL)


def test_partially_null_metric_is_not_flagged():
    issues = validate_result(
        _result(["region", "total"], [("EC", None), ("SC", 100)]), has_filters=False
    )
    assert ValidationCode.ALL_NULL not in _codes(issues)


def test_truncated_result_warns():
    issues = validate_result(_result(["region"], [("EC",)], truncated=True), has_filters=False)

    assert ValidationCode.ROW_COUNT_TRUNCATED in _codes(issues)
    assert [issue.severity for issue in issues if issue.code == ValidationCode.ROW_COUNT_TRUNCATED] == ["warn"]


def test_magnitude_shift_warns_but_does_not_block():
    result = _result(
        ["sales_revenue", "sales_revenue_comparison"], [(1_000_000, 1_000)]
    )
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )

    assert ValidationCode.MAGNITUDE_SHIFT in _codes(issues)
    # Spec 5.5: answer anyway, but say it looks abnormal.
    assert all(issue.severity == "warn" for issue in issues)


def test_moderate_change_is_not_flagged_as_magnitude_shift():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(1200, 1000)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    assert ValidationCode.MAGNITUDE_SHIFT not in _codes(issues)


def test_zero_baseline_does_not_raise():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(500, 0)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    # Growth from zero is unusual but must not crash the validator.
    assert isinstance(issues, tuple)


def test_null_baseline_is_skipped():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(500, None)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    assert ValidationCode.MAGNITUDE_SHIFT not in _codes(issues)


def test_issue_messages_are_user_facing():
    issues = validate_result(_result(["total"], []), has_filters=True)
    assert all(issue.message for issue in issues)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/execution -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.execution'`

- [ ] **Step 4: 写执行层**

`backend/app/execution/runner.py`：

```python
"""Query execution (spec 5.4).

Retries are deliberately narrow: timeouts and connection drops are transient,
everything else is not. A statement that fails on a missing column will fail
identically on retry — under a compiler architecture that failure means the
semantic model and the physical table have drifted, which retrying cannot fix.
"""

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.engine import Connection

from app.core.config import Settings
from app.core.db import sample_engine

_TRANSIENT_MARKERS = (
    "timeout",
    "canceling statement",
    "server closed the connection",
    "connection reset",
    "could not connect",
    "terminating connection",
)


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    truncated: bool
    elapsed_ms: int


class ExecutionFailedError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class _Executable(Protocol):
    sql: str
    row_limit: int


def _classify(error: Exception) -> str:
    message = str(error).lower()
    if any(marker in message for marker in _TRANSIENT_MARKERS):
        return "timeout" if "timeout" in message or "canceling" in message else "connection"
    if isinstance(error, (DBAPIError, SQLAlchemyError)):
        return "sql"
    return "unknown"


def _run_once(connection, sql: str, row_limit: int) -> QueryResult:
    started = time.perf_counter()
    cursor = connection.execute(text(sql))
    rows = cursor.fetchall()
    elapsed = int((time.perf_counter() - started) * 1000)

    return QueryResult(
        columns=tuple(cursor.keys()),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        truncated=len(rows) >= row_limit,
        elapsed_ms=elapsed,
    )


def execute(
    secured: _Executable, settings: Settings, *, connection: Connection | None = None
) -> QueryResult:
    attempts = settings.execution_retry_attempts + 1
    last_kind = "unknown"
    last_detail = ""

    for attempt in range(attempts):
        try:
            if connection is not None:
                return _run_once(connection, secured.sql, secured.row_limit)
            with sample_engine.connect() as own_connection:
                return _run_once(own_connection, secured.sql, secured.row_limit)
        except (OperationalError, SQLAlchemyError) as error:
            last_kind = _classify(error)
            last_detail = f"{error.__class__.__name__}: {error}"
            if last_kind not in ("timeout", "connection"):
                break
            if attempt == attempts - 1:
                break

    raise ExecutionFailedError(last_kind, last_detail)
```

- [ ] **Step 5: 写结果校验**

`backend/app/execution/validation.py`：

```python
"""Result validation (spec M-15).

Blocking issues stop the answer: reporting a number that came from an empty or
all-NULL result is exactly the silent-error failure mode the product exists to
prevent. Warnings still answer, but say what looks off.
"""

from dataclasses import dataclass
from enum import Enum

from app.execution.runner import QueryResult

# A change beyond this multiple against the baseline is treated as suspicious.
_MAGNITUDE_FACTOR = 10.0


class ValidationCode(str, Enum):
    EMPTY_RESULT = "empty_result"
    ALL_NULL = "all_null"
    FILTER_TOO_NARROW = "filter_too_narrow"
    ROW_COUNT_TRUNCATED = "row_count_truncated"
    MAGNITUDE_SHIFT = "magnitude_shift"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: ValidationCode
    severity: str  # "block" | "warn"
    message: str


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_result(
    result: QueryResult,
    *,
    has_filters: bool,
    comparison_columns: dict[str, str] | None = None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []

    if result.row_count == 0:
        if has_filters:
            issues.append(
                ValidationIssue(
                    ValidationCode.FILTER_TOO_NARROW,
                    "block",
                    "当前筛选条件下没有数据，可能是过滤条件过窄，请确认筛选范围",
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    ValidationCode.EMPTY_RESULT,
                    "block",
                    "该时间范围内没有数据",
                )
            )
        return tuple(issues)

    all_null_indexes = [
        index
        for index, _ in enumerate(result.columns)
        if all(row[index] is None for row in result.rows)
    ]
    if all_null_indexes:
        names = "、".join(result.columns[index] for index in all_null_indexes)
        issues.append(
            ValidationIssue(
                ValidationCode.ALL_NULL,
                "block",
                f"字段 {names} 的取值全部为空，结果不可用于结论",
            )
        )

    if result.truncated:
        issues.append(
            ValidationIssue(
                ValidationCode.ROW_COUNT_TRUNCATED,
                "warn",
                f"结果已截断至 {result.row_count} 行，可能不完整",
            )
        )

    for current, baseline in (comparison_columns or {}).items():
        if current not in result.columns or baseline not in result.columns:
            continue
        current_index = result.columns.index(current)
        baseline_index = result.columns.index(baseline)

        for row in result.rows:
            current_value = row[current_index]
            baseline_value = row[baseline_index]
            if not (_is_number(current_value) and _is_number(baseline_value)):
                continue
            if baseline_value == 0:
                continue
            if abs(current_value) > abs(baseline_value) * _MAGNITUDE_FACTOR:
                issues.append(
                    ValidationIssue(
                        ValidationCode.MAGNITUDE_SHIFT,
                        "warn",
                        f"{current} 相比对比期变化超过 {int(_MAGNITUDE_FACTOR)} 倍，建议核对口径",
                    )
                )
                break

    return tuple(issues)
```

- [ ] **Step 6: 在配置中补齐重试次数**

`app/core/config.py` 的 `Settings` 增加：

```python
    execution_retry_attempts: int = 2
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/execution -v`
Expected: PASS（20 项）

- [ ] **Step 8: 运行全部测试**

Run: `cd backend && python -m pytest -v`
Expected: PASS（计划 01 的 25 项 + 计划 02 的 56 项 + 本计划 94 项）

- [ ] **Step 9: 提交**

```bash
git add backend/app/execution backend/tests/execution backend/app/core/config.py
git commit -F - <<'EOF'
实现查询执行与结果校验

编译器架构下「字段不存在」意味着语义模型与物理表已漂移，重试无用，因此重试范围收窄到超时与连接失败两类瞬时故障，其余错误直接归类上报。结果校验则把空结果、全空值这类会导致静默错误的情形设为阻断级，不允许据此作答。

- 执行层只负责发送语句与取回结果，不做任何 SQL 变换
- 仅对超时与连接中断重试，其余错误一次失败即归类上报
- 空结果区分无数据与过滤过窄两种情形，分别给出不同后续动作
- 全空值与空结果为阻断级，截断与量级突变为警告级仍可作答
- 验证：pytest 全量通过，其中执行与校验 20 项
EOF
```

---

## 自查

**Spec 覆盖**（对应设计文档 3.2 第 5~6 步、5.5、5.6、8）：

| Spec 条目 | 承载任务 |
|---|---|
| M-11 行级权限（不由模型实现） | Task 1、2 |
| M-12 列权限与脱敏 | Task 3 |
| M-10 AST 白名单 | Task 4 |
| M-13 成本护栏 | Task 5 |
| 3.2「Verified Query 也必须过安全改写」 | Task 6（`secure_verified_sql`） |
| 5.4 执行失败只重试超时与连接 | Task 7 |
| M-15 结果校验 | Task 7（`validate_result`） |
| 5.6 越权拒答不泄漏元数据 | Task 3（`PermissionDeniedError` + 断言无泄漏） |
| 8 安全项 100% 通过 | Task 2~6 全部为安全测试 |

**安全测试清单**（发布门禁，必须全绿）：

- RLS 在所有路径注入：`test_row_level.py::test_policy_reaches_every_cte_of_a_comparison_query`、`test_pipeline.py::test_verified_sql_also_gets_row_policy`
- 无权列在召回阶段即不可见：`test_columns.py::test_denied_field_is_absent_from_visible_dataset`、`test_metrics_depending_on_denied_fields_are_removed`
- 脱敏生效：`test_columns.py::test_masking_rewrites_the_projection`
- DDL/DML 被拦截：`test_whitelist.py::test_ddl_and_dml_are_rejected`、`test_pipeline.py::test_verified_sql_with_dml_is_rejected`
- 越权拒答不泄漏表名：`test_columns.py::test_permission_error_leaks_no_metadata`

**类型一致性**：`SecuredQuery.sql`/`row_limit` 满足 `runner._Executable` 协议；`SecuredQuery.applied_row_filters`/`masked_field_names` 供计划 04 拼引证块；`validate_result` 的 `comparison_columns` 由计划 04 从 `CompiledQuery.metric_names` 与 `comparison_metric_names` 配对得出。

**对计划 02 的两处回填**：`CompiledQuery.sql_compact`（Task 2 Step 2）与 `DatasetDef.has_metric`（Task 3 Step 4）。实施时若计划 02 已完成，这两处作为独立小改动提交。

## 交付物

完成本计划后：一条编译好的查询可以被安全改写、护栏拦截、执行并校验，得到可信的结果集与需要提示给用户的问题清单。**此时还没有意图识别、澄清与作答**——那是计划 04。

