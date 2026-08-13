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
    # OIDC `sub` claim — the stable key under which a returning user is
    # re-bound to the same row. Nullable so legacy rows from before S1
    # (provisioned by username only) keep loading without constraint errors.
    oidc_subject: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
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