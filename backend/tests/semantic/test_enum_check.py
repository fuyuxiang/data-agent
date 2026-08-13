"""Tests for enum alias conflict detection (S4 P1-08)."""

import pytest


pytestmark = pytest.mark.no_db


def _make_dataset_with_enums():
    """Build a dataset with a region field that has enum values."""
    from app.semantic.model import DatasetDef, FieldDef, MetricDef, EnumValueDef

    fields = (
        FieldDef(
            name="region",
            business_name="地区",
            physical_column="region",
            semantic_type="enum",
            enum_values=(
                EnumValueDef(
                    physical_value="east",
                    business_value="华东",
                    aliases=("东方", "East"),
                ),
                EnumValueDef(
                    physical_value="west",
                    business_value="华西",
                    aliases=("西方", "West"),
                ),
            ),
        ),
        FieldDef(
            name="status",
            business_name="状态",
            physical_column="status",
            semantic_type="enum",
            enum_values=(
                EnumValueDef(physical_value="active", business_value="激活", aliases=("启用",)),
                EnumValueDef(physical_value="inactive", business_value="停用", aliases=("禁用",)),
            ),
        ),
        FieldDef(
            name="amount",
            business_name="金额",
            physical_column="amount",
            semantic_type="measure",
        ),
    )

    metrics = (
        MetricDef(
            name="sales_amount",
            business_name="销售额",
            description="",
            kind="atomic",
            time_field="order_date",
            source_field="amount",
            aggregation="sum",
        ),
    )

    return DatasetDef(
        name="orders",
        business_name="订单",
        grain="day",
        applicable_scenario="",
        forbidden_scenario="",
        physical_table="sample.orders",
        is_published=True,
        metrics=metrics,
        fields=fields,
    )


def _make_dataset_with_conflict():
    """Build a dataset with deliberate alias conflicts for testing."""
    from app.semantic.model import DatasetDef, FieldDef, MetricDef, EnumValueDef

    fields = (
        FieldDef(
            name="region",
            business_name="地区",
            physical_column="region",
            semantic_type="enum",
            enum_values=(
                # Both "east" and "east_region" share the alias "东方"
                EnumValueDef(
                    physical_value="east",
                    business_value="华东",
                    aliases=("东方",),
                ),
                EnumValueDef(
                    physical_value="east_region",
                    business_value="东部地区",
                    aliases=("东方",),  # CONFLICT
                ),
            ),
        ),
        FieldDef(
            name="amount",
            business_name="金额",
            physical_column="amount",
            semantic_type="measure",
        ),
    )

    metrics = (
        MetricDef(
            name="sales_amount",
            business_name="销售额",
            description="",
            kind="atomic",
            time_field="order_date",
            source_field="amount",
            aggregation="sum",
        ),
    )

    return DatasetDef(
        name="orders",
        business_name="订单",
        grain="day",
        applicable_scenario="",
        forbidden_scenario="",
        physical_table="sample.orders",
        is_published=True,
        metrics=metrics,
        fields=fields,
    )


class TestAliasNormalization:
    """Test alias normalization for comparison."""

    def test_normalize_lowercase(self):
        """Case folding converts to lowercase."""
        from app.semantic.enum_check import _normalize_alias

        assert _normalize_alias("East") == "east"
        assert _normalize_alias("EAST") == "east"

    def test_normalize_whitespace_stripped(self):
        """Whitespace is removed from normalized form."""
        from app.semantic.enum_check import _normalize_alias

        assert _normalize_alias(" east ") == "east"
        assert _normalize_alias("e a s t") == "east"
        assert _normalize_alias("east\n") == "east"

    def test_normalize_unicode_nfkc(self):
        """NFKC normalisation handles compatibility forms."""
        from app.semantic.enum_check import _normalize_alias

        # Fullwidth 'a' (U+FF41) normalises to 'a'
        assert _normalize_alias("ａ") == "a"


class TestFindAliasConflicts:
    """Test find_alias_conflicts() detection."""

    def test_no_conflicts_clean_dataset(self):
        """Clean dataset has no alias conflicts."""
        from app.semantic.enum_check import find_alias_conflicts

        dataset = _make_dataset_with_enums()
        conflicts = find_alias_conflicts(dataset)

        assert conflicts == ()

    def test_detect_exact_alias_conflict(self):
        """Detect when two values share the exact same alias."""
        from app.semantic.enum_check import find_alias_conflicts

        dataset = _make_dataset_with_conflict()
        conflicts = find_alias_conflicts(dataset)

        # Should detect 1 conflict (the shared "东方" alias)
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict.field_name == "region"
        assert conflict.alias_normalized == "东方"
        assert len(conflict.competing_values) == 2

    def test_no_conflict_when_aliases_differ(self):
        """Different aliases do not conflict."""
        from app.semantic.model import DatasetDef, FieldDef, MetricDef, EnumValueDef
        from app.semantic.enum_check import find_alias_conflicts

        fields = (
            FieldDef(
                name="region",
                business_name="地区",
                physical_column="region",
                semantic_type="enum",
                enum_values=(
                    EnumValueDef(physical_value="east", business_value="华东", aliases=("东方",)),
                    EnumValueDef(physical_value="west", business_value="华西", aliases=("西方",)),
                ),
            ),
            FieldDef(name="x", business_name="x", physical_column="x", semantic_type="text"),
        )
        metrics = (
            MetricDef(name="m", business_name="m", description="", kind="atomic",
                      time_field="t", source_field="x", aggregation="sum"),
        )
        dataset = DatasetDef(
            name="d", business_name="d", grain="day",
            applicable_scenario="", forbidden_scenario="",
            physical_table="t", is_published=True,
            metrics=metrics, fields=fields,
        )

        conflicts = find_alias_conflicts(dataset)
        assert conflicts == ()

    def test_conflict_describe_includes_physical_values(self):
        """Conflict description includes physical values."""
        from app.semantic.enum_check import EnumAliasConflict
        from app.semantic.model import EnumValueDef

        v1 = EnumValueDef(physical_value="a", business_value="A", aliases=())
        v2 = EnumValueDef(physical_value="b", business_value="B", aliases=())

        conflict = EnumAliasConflict(
            field_name="region",
            alias_normalized="shared",
            competing_values=(v1, v2),
        )

        text = conflict.describe()

        assert "region" in text
        assert "shared" in text
        assert "a" in text
        assert "b" in text


class TestAssertNoConflicts:
    """Test assert_no_alias_conflicts() (publish gate)."""

    def test_clean_dataset_passes(self):
        """Clean dataset passes the publish gate."""
        from app.semantic.enum_check import assert_no_alias_conflicts

        dataset = _make_dataset_with_enums()
        assert_no_alias_conflicts(dataset)  # No exception

    def test_conflict_raises(self):
        """Conflict raises EnumConflictError."""
        from app.semantic.enum_check import (
            EnumConflictError,
            assert_no_alias_conflicts,
        )

        dataset = _make_dataset_with_conflict()

        with pytest.raises(EnumConflictError):
            assert_no_alias_conflicts(dataset)

    def test_error_message_mentions_field(self):
        """Error message identifies the field and alias."""
        from app.semantic.enum_check import (
            EnumConflictError,
            assert_no_alias_conflicts,
        )

        dataset = _make_dataset_with_conflict()

        with pytest.raises(EnumConflictError) as exc_info:
            assert_no_alias_conflicts(dataset)

        message = str(exc_info.value)
        assert "region" in message
        assert "东方" in message


class TestResolveEnumAll:
    """Test multi-candidate resolution."""

    def test_unique_match(self):
        """Unique match returns the single candidate."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        result = resolve_enum_all(dataset, "region", "华东")

        assert result.is_unique
        assert result.unique_value() == "east"
        assert len(result.candidates) == 1

    def test_no_match(self):
        """No match returns empty candidates."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        result = resolve_enum_all(dataset, "region", "不存在的地区")

        assert result.is_empty
        assert result.unique_value() is None
        assert result.candidates == ()

    def test_ambiguous_match_returns_all_candidates(self):
        """Ambiguous match returns all candidates (caller must clarify)."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_conflict()
        # "东方" matches both east and east_region
        result = resolve_enum_all(dataset, "region", "东方")

        assert result.is_ambiguous
        assert len(result.candidates) == 2
        # Caller should not get a value back when ambiguous
        assert result.unique_value() is None

    def test_match_by_alias(self):
        """Match by alias (not just business_value)."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        # "East" is an alias for east
        result = resolve_enum_all(dataset, "region", "East")

        assert result.is_unique
        assert result.unique_value() == "east"

    def test_match_case_insensitive(self):
        """Match is case-insensitive."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        result = resolve_enum_all(dataset, "region", "EAST")

        assert result.is_unique
        assert result.unique_value() == "east"

    def test_unknown_field_returns_empty(self):
        """Unknown field returns empty."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        result = resolve_enum_all(dataset, "nonexistent_field", "any")

        assert result.is_empty

    def test_field_without_enums_returns_empty(self):
        """Field without enum values returns empty."""
        from app.semantic.enum_check import resolve_enum_all

        dataset = _make_dataset_with_enums()
        result = resolve_enum_all(dataset, "amount", "100")

        assert result.is_empty


class TestEnumSecurityIntegration:
    """Test enum check integrates with publish gate."""

    def test_unpublished_dataset_can_have_conflicts(self):
        """Drafts may have conflicts; the publish gate catches them."""
        from app.semantic.enum_check import find_alias_conflicts
        from app.semantic.model import DatasetDef, FieldDef, MetricDef, EnumValueDef

        # Build a draft with conflicts
        fields = (
            FieldDef(
                name="x",
                business_name="x",
                physical_column="x",
                semantic_type="enum",
                enum_values=(
                    EnumValueDef(physical_value="a", business_value="A", aliases=("shared",)),
                    EnumValueDef(physical_value="b", business_value="B", aliases=("shared",)),
                ),
            ),
            FieldDef(name="y", business_name="y", physical_column="y", semantic_type="text"),
        )
        metrics = (
            MetricDef(name="m", business_name="m", description="", kind="atomic",
                      time_field="t", source_field="y", aggregation="sum"),
        )
        dataset = DatasetDef(
            name="draft", business_name="draft", grain="day",
            applicable_scenario="", forbidden_scenario="",
            physical_table="t", is_published=False,  # Draft, not published
            metrics=metrics, fields=fields,
        )

        # The lint should still find the conflict even on a draft
        conflicts = find_alias_conflicts(dataset)
        assert len(conflicts) == 1
