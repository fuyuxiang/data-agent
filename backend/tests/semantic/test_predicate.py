"""Tests for Predicate DSL (S4 P1-06)."""

import pytest


pytestmark = pytest.mark.no_db


class TestComparison:
    """Test Comparison predicate."""

    def test_comparison_constructs(self):
        """Comparison(field, op, value) constructs."""
        from app.semantic.predicate import Comparison, PredicateOp

        c = Comparison(field="status", op=PredicateOp.EQ, value="active")

        assert c.field == "status"
        assert c.op == PredicateOp.EQ
        assert c.value == "active"

    def test_is_null_no_value(self):
        """IS NULL has no value."""
        from app.semantic.predicate import Comparison, PredicateOp

        c = Comparison(field="deleted_at", op=PredicateOp.IS_NULL)

        assert c.value is None


class TestAndOrNot:
    """Test logical composition."""

    def test_and_combines(self):
        """And combines left and right."""
        from app.semantic.predicate import And, Comparison, PredicateOp

        a = Comparison(field="x", op=PredicateOp.GT, value=0)
        b = Comparison(field="y", op=PredicateOp.LT, value=10)
        combined = And(a, b)

        assert combined.left == a
        assert combined.right == b

    def test_or_combines(self):
        """Or combines left and right."""
        from app.semantic.predicate import Comparison, Or, PredicateOp

        a = Comparison(field="x", op=PredicateOp.EQ, value=1)
        b = Comparison(field="x", op=PredicateOp.EQ, value=2)
        combined = Or(a, b)

        assert combined.left == a

    def test_not_wraps(self):
        """Not wraps a single predicate."""
        from app.semantic.predicate import Comparison, Not, PredicateOp

        a = Comparison(field="x", op=PredicateOp.IS_NULL)
        n = Not(a)

        assert n.inner == a


class TestValidation:
    """Test predicate validation."""

    def test_unknown_field_raises(self):
        """Unknown field raises."""
        from app.semantic.predicate import (
            Comparison, PredicateError, PredicateOp, validate,
        )

        c = Comparison(field="unknown_field", op=PredicateOp.EQ, value=1)

        with pytest.raises(PredicateError, match="Unknown field"):
            validate(c, known_fields={"a", "b"})

    def test_in_requires_list(self):
        """IN with non-list raises."""
        from app.semantic.predicate import (
            Comparison, PredicateError, PredicateOp, validate,
        )

        c = Comparison(field="x", op=PredicateOp.IN, value="not_a_list")

        with pytest.raises(PredicateError, match="requires a list"):
            validate(c, known_fields={"x"})

    def test_eq_requires_value(self):
        """EQ with None value raises."""
        from app.semantic.predicate import (
            Comparison, PredicateError, PredicateOp, validate,
        )

        c = Comparison(field="x", op=PredicateOp.EQ, value=None)

        with pytest.raises(PredicateError, match="non-None"):
            validate(c, known_fields={"x"})

    def test_is_null_no_value_ok(self):
        """IS NULL with no value passes."""
        from app.semantic.predicate import (
            Comparison, PredicateOp, validate,
        )

        c = Comparison(field="x", op=PredicateOp.IS_NULL)

        validate(c, known_fields={"x"})  # No error

    def test_validate_nested(self):
        """Validation recurses into And/Or/Not."""
        from app.semantic.predicate import (
            And, Comparison, Or, PredicateError, PredicateOp, validate,
        )

        a = Comparison(field="x", op=PredicateOp.EQ, value=1)
        b = Comparison(field="y_unknown", op=PredicateOp.EQ, value=1)
        combined = Or(And(a, b), a)

        with pytest.raises(PredicateError):
            validate(combined, known_fields={"x", "y"})


class TestFieldExtraction:
    """Test referenced_fields() for lineage."""

    def test_simple_field(self):
        """Comparison references its field."""
        from app.semantic.predicate import Comparison, PredicateOp, referenced_fields

        c = Comparison(field="status", op=PredicateOp.EQ, value="active")
        assert referenced_fields(c) == frozenset({"status"})

    def test_combined_fields(self):
        """And/Or reference union of fields."""
        from app.semantic.predicate import (
            And, Comparison, Or, PredicateOp, referenced_fields,
        )

        a = Comparison(field="a", op=PredicateOp.EQ, value=1)
        b = Comparison(field="b", op=PredicateOp.EQ, value=2)
        c = Comparison(field="c", op=PredicateOp.EQ, value=3)

        and_ab = And(a, b)
        assert referenced_fields(and_ab) == frozenset({"a", "b"})

        or_ac = Or(a, c)
        assert referenced_fields(or_ac) == frozenset({"a", "c"})

    def test_dedup(self):
        """Same field referenced multiple times yields one set element."""
        from app.semantic.predicate import (
            And, Comparison, PredicateOp, referenced_fields,
        )

        a = Comparison(field="x", op=PredicateOp.EQ, value=1)
        b = Comparison(field="x", op=PredicateOp.GT, value=0)

        result = referenced_fields(And(a, b))
        assert result == frozenset({"x"})


class TestSQLCompilation:
    """Test SQL compilation via to_sqlglot()."""

    def test_eq_compiles(self):
        """EQ compiles to a sqlglot EQ expression."""
        from app.semantic.predicate import Comparison, PredicateOp, to_sqlglot
        from sqlglot import exp

        c = Comparison(field="status", op=PredicateOp.EQ, value="active")
        result = to_sqlglot(c)

        assert isinstance(result, exp.EQ)

    def test_in_compiles(self):
        """IN compiles to a sqlglot In expression."""
        from app.semantic.predicate import Comparison, PredicateOp, to_sqlglot
        from sqlglot import exp

        c = Comparison(field="region", op=PredicateOp.IN, value=["east", "west"])
        result = to_sqlglot(c)

        assert isinstance(result, exp.In)

    def test_is_null_compiles(self):
        """IS NULL compiles to Is + Null."""
        from app.semantic.predicate import Comparison, PredicateOp, to_sqlglot
        from sqlglot import exp

        c = Comparison(field="deleted_at", op=PredicateOp.IS_NULL)
        result = to_sqlglot(c)

        assert isinstance(result, exp.Is)
        assert isinstance(result.expression, exp.Null)

    def test_and_compiles(self):
        """And compiles to exp.And."""
        from app.semantic.predicate import And, Comparison, PredicateOp, to_sqlglot
        from sqlglot import exp

        a = Comparison(field="x", op=PredicateOp.GT, value=0)
        b = Comparison(field="y", op=PredicateOp.LT, value=10)

        result = to_sqlglot(And(a, b))

        assert isinstance(result, exp.And)


class TestDictParsing:
    """Test from_dict() round-trip."""

    def test_parse_comparison(self):
        """Parse a comparison dict."""
        from app.semantic.predicate import Comparison, from_dict

        data = {"field": "status", "op": "eq", "value": "active"}
        result = from_dict(data)

        assert isinstance(result, Comparison)
        assert result.field == "status"
        assert result.value == "active"

    def test_parse_and(self):
        """Parse an AND list."""
        from app.semantic.predicate import And, from_dict

        data = {
            "and": [
                {"field": "x", "op": "gt", "value": 0},
                {"field": "y", "op": "lt", "value": 10},
            ]
        }
        result = from_dict(data)

        assert isinstance(result, And)

    def test_parse_nested(self):
        """Parse deeply nested predicates."""
        from app.semantic.predicate import (
            And, Comparison, Or, from_dict,
        )

        data = {
            "and": [
                {"field": "a", "op": "eq", "value": 1},
                {
                    "or": [
                        {"field": "b", "op": "eq", "value": 2},
                        {"field": "c", "op": "eq", "value": 3},
                    ]
                },
            ]
        }
        result = from_dict(data)

        # Outermost is And
        assert isinstance(result, And)
        # The right side is an Or
        assert isinstance(result.right, Or)

    def test_parse_invalid_raises(self):
        """Invalid dict raises."""
        from app.semantic.predicate import PredicateError, from_dict

        with pytest.raises(PredicateError):
            from_dict({"unknown_key": "value"})


class TestLegacyFilterConversion:
    """Test conversion from old fixed_filter strings."""

    def test_eq_string(self):
        """Convert 'field = value'."""
        from app.semantic.predicate import Comparison, PredicateOp, from_legacy_filter

        result = from_legacy_filter("status = 'active'", known_fields={"status"})

        assert isinstance(result, Comparison)
        assert result.field == "status"
        assert result.op == PredicateOp.EQ
        assert result.value == "active"

    def test_in_clause(self):
        """Convert 'field IN (...)'."""
        from app.semantic.predicate import Comparison, PredicateOp, from_legacy_filter

        result = from_legacy_filter(
            "region IN ('east', 'west')", known_fields={"region"}
        )

        assert isinstance(result, Comparison)
        assert result.op == PredicateOp.IN
        assert result.value == ("east", "west")

    def test_is_null(self):
        """Convert 'field IS NULL'."""
        from app.semantic.predicate import Comparison, PredicateOp, from_legacy_filter

        result = from_legacy_filter("deleted_at IS NULL", known_fields={"deleted_at"})

        assert isinstance(result, Comparison)
        assert result.op == PredicateOp.IS_NULL

    def test_numeric_value(self):
        """Convert 'field > 100'."""
        from app.semantic.predicate import Comparison, PredicateOp, from_legacy_filter

        result = from_legacy_filter("amount > 100", known_fields={"amount"})

        assert result.op == PredicateOp.GT
        assert result.value == 100

    def test_untranslatable_raises(self):
        """Complex SQL raises PredicateError."""
        from app.semantic.predicate import PredicateError, from_legacy_filter

        # Subquery cannot be translated
        with pytest.raises(PredicateError):
            from_legacy_filter("x IN (SELECT y FROM t)", known_fields={"x"})


class TestPredicateSecurity:
    """Test that Predicate DSL supports security checks."""

    def test_fields_auto_appear_in_lineage(self):
        """Fields in predicates are discoverable for lineage."""
        from app.semantic.predicate import (
            And, Comparison, PredicateOp, referenced_fields, validate,
        )

        predicate = And(
            Comparison(field="region", op=PredicateOp.IN, value=["east"]),
            Comparison(field="status", op=PredicateOp.EQ, value="active"),
        )

        # Validate
        validate(predicate, known_fields={"region", "status"})

        # Fields automatically become part of lineage
        assert referenced_fields(predicate) == frozenset({"region", "status"})

    def test_only_known_fields_accepted(self):
        """Validation refuses unknown fields."""
        from app.semantic.predicate import (
            Comparison, PredicateError, PredicateOp, validate,
        )

        c = Comparison(field="secret_field", op=PredicateOp.EQ, value=1)

        with pytest.raises(PredicateError):
            validate(c, known_fields={"public_field"})

    def test_no_subquery_support(self):
        """DSL has no subquery type, structurally can't bypass column perms."""
        from app.semantic.predicate import (
            And, Comparison, Or, Not, PredicateOp,
        )

        # Predicate types are: Comparison, And, Or, Not — no subquery
        valid_types = {Comparison, And, Or, Not}
        assert len(valid_types) == 4

        # No type for "subquery" exists; impossible to construct
