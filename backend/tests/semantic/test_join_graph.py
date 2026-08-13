"""Tests for Join Graph (S4 P2-01)."""

import pytest


pytestmark = pytest.mark.no_db


def _make_relation(
    *,
    id: str = "rel_1",
    left: str = "orders",
    right: str = "customers",
    left_col: str = "customer_id",
    right_col: str = "id",
    cardinality="one_to_many",
    fanout_risk="none",
    is_default: bool = True,
) -> "Relation":
    """Build a sample relation for testing."""
    from app.semantic.relation import Relation, JoinKey, Cardinality, FanoutRisk

    return Relation(
        id=id,
        left_dataset=left,
        right_dataset=right,
        join_keys=(JoinKey(left_column=left_col, right_column=right_col),),
        cardinality=Cardinality(cardinality),
        fanout_risk=FanoutRisk(fanout_risk),
        is_default_path=is_default,
    )


class TestRelationConstruction:
    """Test Relation data structure."""

    def test_relation_constructs(self):
        """Relation with required fields constructs."""
        from app.semantic.relation import Relation, JoinKey, Cardinality, Optionality

        rel = Relation(
            id="o2c",
            left_dataset="orders",
            right_dataset="customers",
            join_keys=(JoinKey(left_column="customer_id", right_column="id"),),
            cardinality=Cardinality.ONE_TO_MANY,
            optionality=Optionality.INNER,
            fanout_risk="none",
        )

        assert rel.id == "o2c"
        assert rel.left_dataset == "orders"
        assert rel.cardinality == Cardinality.ONE_TO_MANY

    def test_relation_default_optionality(self):
        """Default optionality is INNER."""
        from app.semantic.relation import Relation, JoinKey, Cardinality, Optionality

        rel = Relation(
            id="r",
            left_dataset="a",
            right_dataset="b",
            join_keys=(JoinKey("a.x", "b.x"),),
            cardinality=Cardinality.ONE_TO_ONE,
        )
        assert rel.optionality == Optionality.INNER

    def test_join_key_constructs(self):
        """JoinKey with two column names."""
        from app.semantic.relation import JoinKey

        k = JoinKey(left_column="l.x", right_column="r.x")
        assert k.left_column == "l.x"
        assert k.right_column == "r.x"


class TestJoinPathDirect:
    """Test direct (1-hop) join paths."""

    def test_direct_path_exists(self):
        """Direct relation between two datasets is found."""
        from app.semantic.join_graph import build_join_graph

        graph = build_join_graph([_make_relation()])
        path = graph.find_path("orders", "customers")

        assert path.start == "orders"
        assert path.end == "customers"
        assert path.total_hops == 1
        assert path.relations[0].id == "rel_1"

    def test_direct_path_in_reverse(self):
        """Reverse direction is also found."""
        from app.semantic.join_graph import build_join_graph

        graph = build_join_graph([_make_relation()])
        path = graph.find_path("customers", "orders")

        assert path.total_hops == 1

    def test_same_dataset_is_zero_hop(self):
        """Same dataset has zero-hop path."""
        from app.semantic.join_graph import build_join_graph

        graph = build_join_graph([_make_relation()])
        path = graph.find_path("orders", "orders")

        assert path.total_hops == 0
        assert path.relations == ()


class TestJoinPathTwoHop:
    """Test multi-hop (2-hop) join paths."""

    def test_two_hop_path(self):
        """Two-hop path through intermediate dataset."""
        from app.semantic.join_graph import build_join_graph

        # orders -> customers -> regions
        rel1 = _make_relation(id="o2c", left="orders", right="customers",
                              left_col="customer_id", right_col="id")
        rel2 = _make_relation(id="c2r", left="customers", right="regions",
                              left_col="region_id", right_col="id",
                              cardinality="one_to_one")

        graph = build_join_graph([rel1, rel2])
        path = graph.find_path("orders", "regions")

        assert path.total_hops == 2
        assert path.relations[0].id == "o2c"
        assert path.relations[1].id == "c2r"

    def test_three_hop_raises(self):
        """Three-hop path is rejected (max 2)."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import NoPathError

        rel1 = _make_relation(id="a2b", left="a", right="b",
                              left_col="x", right_col="id")
        rel2 = _make_relation(id="b2c", left="b", right="c",
                              left_col="x", right_col="id")
        rel3 = _make_relation(id="c2d", left="c", right="d",
                              left_col="x", right_col="id")

        graph = build_join_graph([rel1, rel2, rel3], max_hops=2)

        with pytest.raises(NoPathError):
            graph.find_path("a", "d")


class TestMultiplePaths:
    """Test multiple-path handling."""

    def test_multiple_paths_with_default(self):
        """Multiple 1-hop paths but one default is selected."""
        from app.semantic.join_graph import build_join_graph

        # Two direct 1-hop relations between orders and regions
        rel1 = _make_relation(id="by_region_id", left="orders", right="regions",
                              left_col="region_id", right_col="id",
                              is_default=False)
        rel2 = _make_relation(id="by_postal", left="orders", right="regions",
                              left_col="postal_code", right_col="postal",
                              is_default=True)
        # A longer 2-hop path (orders -> customers -> regions) — not preferred

        graph = build_join_graph([rel1, rel2])
        path = graph.find_path("orders", "regions")

        # Default 1-hop path is selected
        assert path.total_hops == 1
        assert path.relations[0].id == "by_postal"

    def test_multiple_paths_no_default_raises(self):
        """Multiple 1-hop paths with no default raise error."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import MultiplePathsError

        rel1 = _make_relation(id="by_region_id", left="orders", right="regions",
                              left_col="region_id", right_col="id",
                              is_default=False)
        rel2 = _make_relation(id="by_postal", left="orders", right="regions",
                              left_col="postal_code", right_col="postal",
                              is_default=False)

        graph = build_join_graph([rel1, rel2])

        with pytest.raises(MultiplePathsError) as exc_info:
            graph.find_path("orders", "regions")

        # Both relations should be mentioned in the error
        assert "by_region_id" in str(exc_info.value)
        assert "by_postal" in str(exc_info.value)

    def test_two_hop_is_preferred_over_one_hop_when_shorter(self):
        """1-hop is preferred over 2-hop (BFS)."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import MultiplePathsError

        # Two 1-hop paths: by_region_id and by_postal
        rel1 = _make_relation(id="by_region_id", left="orders", right="regions",
                              left_col="region_id", right_col="id",
                              is_default=False)
        rel2 = _make_relation(id="by_postal", left="orders", right="regions",
                              left_col="postal_code", right_col="postal",
                              is_default=False)
        # And a 2-hop path (longer, not preferred)
        rel3 = _make_relation(id="o2c", left="orders", right="customers",
                              left_col="customer_id", right_col="id")
        rel4 = _make_relation(id="c2r", left="customers", right="regions",
                              left_col="region_id", right_col="id")

        graph = build_join_graph([rel1, rel2, rel3, rel4])
        # 1-hop paths exist but have no default — should error
        # (2-hop is never the answer if 1-hop exists and is unambiguous)
        with pytest.raises(MultiplePathsError):
            graph.find_path("orders", "regions")


class TestFanoutDetection:
    """Test fanout risk detection."""

    def test_no_fanout_when_not_measure_side(self):
        """Fanout is not flagged when caller is not aggregating measures."""
        from app.semantic.join_graph import build_join_graph

        rel = _make_relation(fanout_risk="measure_duplication")
        graph = build_join_graph([rel])

        # Without is_measure_side, fanout not flagged at path level
        # (this is a UI concern, not a graph concern; fanout is reported
        # by the caller when aggregating)
        path = graph.find_path("orders", "customers")
        assert path.relations[0].fanout_risk.value == "measure_duplication"

    def test_fanout_risk_marked_on_relation(self):
        """Relation can carry fanout_risk flag."""
        from app.semantic.relation import FanoutRisk

        rel = _make_relation(fanout_risk="measure_duplication")
        assert rel.fanout_risk == FanoutRisk.MEASURE_DUPLICATION


class TestJoinGraphValidation:
    """Test Join Graph structural validation."""

    def test_unique_relation_ids(self):
        """Duplicate relation ids fail validation."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import RelationError

        rel1 = _make_relation(id="dup")
        rel2 = _make_relation(id="dup", left="other", right="another",
                              left_col="a", right_col="b")

        with pytest.raises(RelationError):
            build_join_graph([rel1, rel2])

    def test_empty_join_keys_fails(self):
        """Relation with no join keys fails validation."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import RelationError

        # Build a relation with no join keys directly (bypass factory)
        from app.semantic.relation import Relation, JoinKey, Cardinality

        rel = Relation(
            id="no_keys",
            left_dataset="a",
            right_dataset="b",
            join_keys=(),  # Empty!
            cardinality=Cardinality.ONE_TO_ONE,
        )

        with pytest.raises(RelationError):
            build_join_graph([rel])

    def test_validation_passes_for_clean_graph(self):
        """Clean graph passes validation."""
        from app.semantic.join_graph import build_join_graph

        rel1 = _make_relation(id="r1")
        rel2 = _make_relation(id="r2", left="customers", right="regions",
                              left_col="region_id", right_col="id",
                              cardinality="one_to_one")

        # Should not raise
        build_join_graph([rel1, rel2])

    def test_no_path_raises(self):
        """No path raises NoPathError."""
        from app.semantic.join_graph import build_join_graph
        from app.semantic.relation import NoPathError

        rel = _make_relation(left="a", right="b")
        graph = build_join_graph([rel])

        with pytest.raises(NoPathError):
            graph.find_path("a", "c")


class TestJoinGraphPathProperties:
    """Test JoinPath helper properties."""

    def test_datasets_in_path(self):
        """datasets property lists all datasets in order."""
        from app.semantic.join_graph import build_join_graph

        rel1 = _make_relation(id="o2c", left="orders", right="customers",
                              left_col="customer_id", right_col="id")
        rel2 = _make_relation(id="c2r", left="customers", right="regions",
                              left_col="region_id", right_col="id",
                              cardinality="one_to_one")

        graph = build_join_graph([rel1, rel2])
        path = graph.find_path("orders", "regions")

        assert path.datasets == ("orders", "customers", "regions")

    def test_total_hops_count(self):
        """total_hops equals number of relations."""
        from app.semantic.join_graph import build_join_graph

        rel1 = _make_relation(id="o2c", left="orders", right="customers",
                              left_col="customer_id", right_col="id")
        rel2 = _make_relation(id="c2r", left="customers", right="regions",
                              left_col="region_id", right_col="id",
                              cardinality="one_to_one")

        graph = build_join_graph([rel1, rel2])
        path = graph.find_path("orders", "regions")

        assert path.total_hops == 2


class TestJoinGraphCycleAvoidance:
    """Test that cycles in the graph don't cause infinite loops."""

    def test_cycle_does_not_loop(self):
        """Cycle in graph is avoided (visited set)."""
        from app.semantic.join_graph import build_join_graph

        # a -> b -> c -> a (cycle); but BFS must terminate
        rel_ab = _make_relation(id="ab", left="a", right="b",
                                left_col="x", right_col="id")
        rel_bc = _make_relation(id="bc", left="b", right="c",
                                left_col="x", right_col="id")
        rel_ca = _make_relation(id="ca", left="c", right="a",
                                left_col="x", right_col="id")

        graph = build_join_graph([rel_ab, rel_bc, rel_ca], max_hops=2)

        # a -> b is direct
        path = graph.find_path("a", "b")
        assert path.total_hops == 1
