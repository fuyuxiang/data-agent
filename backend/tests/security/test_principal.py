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
    assert analyst.column_access(dataset.field("customer_name"), "orders") == ColumnAccess.MASK
    assert analyst.column_access(dataset.field("amount"), "orders") == ColumnAccess.ALLOW


def test_admin_reads_sensitive_columns(principals):
    dataset = load_dataset(principals, "orders")
    admin = load_principal(principals, "admin")
    assert admin.column_access(dataset.field("customer_name"), "orders") == ColumnAccess.ALLOW


def test_explicit_column_policy_overrides_sensitivity(principals):
    dataset = load_dataset(principals, "orders")
    # east_manager clears the sensitivity bar but cost is explicitly denied.
    principal = load_principal(principals, "east_manager")

    assert principal.column_access(dataset.field("customer_name"), "orders") == ColumnAccess.ALLOW
    assert principal.column_access(dataset.field("cost"), "orders") == ColumnAccess.DENY


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


def test_column_access_does_not_cross_datasets(principals):
    """A DENY scoped to dataset A must not leak to a same-named field in dataset B.

    Before the fix, ``column_access`` would scan every override by field name
    when ``dataset_name`` was empty, letting a DENY on ``orders.cost`` deny
    ``refunds.cost`` as well — a fail-open privilege escalation.
    """
    from app.semantic.enums import Aggregation, SemanticType
    from app.semantic.loader import load_dataset
    from app.semantic.orm import DatasetRow, FieldRow
    from app.security.orm import ColumnPolicyRow, RoleRow, UserRow

    strict_role = RoleRow(
        name="strict_reader",
        business_name="严格读取",
        max_sensitivity=Sensitivity.PUBLIC.value,
        column_policies=[
            ColumnPolicyRow(dataset_name="orders", field_name="cost", access="deny"),
        ],
    )
    refund_user = UserRow(username="refund_user", display_name="退款用户", roles=[strict_role])
    refunds = DatasetRow(
        name="refunds",
        business_name="退款",
        physical_table="sample.refunds",
        fields=[
            FieldRow(
                name="cost",
                physical_column="cost",
                semantic_type=SemanticType.AMOUNT.value,
                default_aggregation=Aggregation.SUM.value,
                allowed_aggregations=[Aggregation.SUM.value, Aggregation.AVG.value],
                sensitivity=Sensitivity.SENSITIVE.value,
            ),
        ],
    )
    principals.add_all([refund_user, refunds])
    principals.flush()

    principal = load_principal(principals, "refund_user")
    refunds_def = load_dataset(principals, "refunds")
    # Strict reader has cost = DENY on orders, but refunds.cost falls back to
    # sensitivity judgment. SENSITIVE > PUBLIC → MASK (not the cross-dataset DENY).
    assert principal.column_access(refunds_def.field("cost"), "refunds") == ColumnAccess.MASK


def test_multi_role_deny_wins_on_overlapping_policies(principals):
    """When one role grants ALLOW and another denies the same column, DENY wins."""
    from app.security.orm import ColumnPolicyRow, RoleRow, UserRow

    role_a = RoleRow(
        name="granting_role",
        business_name="放行角色",
        max_sensitivity=Sensitivity.SENSITIVE.value,
        column_policies=[
            ColumnPolicyRow(dataset_name="orders", field_name="cost", access="allow"),
        ],
    )
    role_b = RoleRow(
        name="blocking_role",
        business_name="拒绝角色",
        max_sensitivity=Sensitivity.SENSITIVE.value,
        column_policies=[
            ColumnPolicyRow(dataset_name="orders", field_name="cost", access="deny"),
        ],
    )
    conflicted = UserRow(
        username="conflicted", display_name="冲突用户", roles=[role_a, role_b]
    )
    principals.add(conflicted)
    principals.flush()

    dataset = load_dataset(principals, "orders")
    principal = load_principal(principals, "conflicted")
    assert principal.column_access(dataset.field("cost"), "orders") == ColumnAccess.DENY