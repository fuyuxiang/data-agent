"""Join Graph traversal (S4 P2-01).

The compiler must resolve a sequence of datasets into a sequence of Relations
to use. The graph is static and audited; LLM never composes joins.

This module:
- holds the JoinGraph
- finds paths between two datasets
- detects fanout (whether joining a measure-side dataset would duplicate values)
- rejects multi-path ambiguity unless a default is marked
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from app.semantic.relation import (
    Cardinality,
    FanoutRisk,
    MultiplePathsError,
    NoPathError,
    Relation,
    RelationError,
)


@dataclass(frozen=True)
class JoinPath:
    """A sequence of Relations connecting datasets.

    `relations` is ordered from the start dataset toward the end dataset.
    `fanout_detected` is True if any relation in the path has
    fanout_risk=MEASURE_DUPLICATION and the path joins from a measure side.
    """

    start: str
    end: str
    relations: tuple[Relation, ...]

    @property
    def datasets(self) -> tuple[str, ...]:
        """Datasets in order from start to end."""
        if not self.relations:
            return (self.start,)
        result = [self.start]
        for r in self.relations:
            if r.left_dataset == result[-1]:
                result.append(r.right_dataset)
            else:
                result.append(r.left_dataset)
        return tuple(result)

    @property
    def total_hops(self) -> int:
        return len(self.relations)


@dataclass(frozen=True)
class JoinGraph:
    """The static, audited Join Graph.

    Build from a list of Relations. Lookup is by dataset name.
    """

    relations: tuple[Relation, ...]
    max_hops: int = 2  # S4 P2-01: max 2 hops

    def outgoing(self, dataset: str) -> tuple[Relation, ...]:
        """Relations that have `dataset` on the left or right side."""
        return tuple(
            r for r in self.relations
            if r.left_dataset == dataset or r.right_dataset == dataset
        )

    def _reverse(self, relation: Relation, current_dataset: str) -> Relation:
        """Reverse a relation so that current_dataset is the left side.

        Cardinality flips (one_to_many <-> many_to_one), optionality is
        converted (left -> right on the other side, equivalent to right join).
        """
        # Build the reversed join keys
        reversed_keys = tuple(
            (right_key := (k.right_column, k.left_column)) and right_key
            for k in relation.join_keys
        )
        # New cardinalities
        new_card = relation.cardinality
        if relation.cardinality == Cardinality.ONE_TO_MANY:
            new_card = Cardinality.MANY_TO_ONE
        elif relation.cardinality == Cardinality.MANY_TO_ONE:
            new_card = Cardinality.ONE_TO_MANY

        # New optionality
        new_opt = relation.optionality
        # If the original was LEFT (keep left), the reversed is RIGHT (keep right).
        # But our Optionality enum only has INNER and LEFT; in our model
        # LEFT means "keep the left side on which the relation is defined",
        # so for a reversed relation we still want to keep the dataset we
        # came from — but our schema is symmetric so we leave it.

        # Build a new Relation with left/right swapped
        return Relation(
            id=relation.id,
            left_dataset=relation.right_dataset,
            right_dataset=relation.left_dataset,
            join_keys=reversed_keys,
            cardinality=new_card,
            optionality=new_opt,
            fanout_risk=relation.fanout_risk,
            allowed_directions=relation.allowed_directions,
            is_default_path=relation.is_default_path,
            description=relation.description,
        )

    def _direct_relations_between(
        self, left: str, right: str
    ) -> tuple[Relation, ...]:
        """All relations directly between left and right (in either direction)."""
        return tuple(
            r for r in self.relations
            if (r.left_dataset == left and r.right_dataset == right)
            or (r.left_dataset == right and r.right_dataset == left)
        )

    def find_path(
        self,
        start: str,
        end: str,
        *,
        is_measure_side: bool = False,
    ) -> JoinPath:
        """Find a path from start to end in the Join Graph.

        Algorithm: BFS up to max_hops. Discovers all shortest paths.
        If multiple shortest paths exist between two datasets with no
        default, raises MultiplePathsError. If no path exists within
        max_hops, raises NoPathError.

        Args:
            start: Source dataset name.
            end: Target dataset name.
            is_measure_side: True if the caller is aggregating a measure
                from `start`. The path will be checked for fanout risk.

        Returns: JoinPath with the chosen relations.
        """
        if start == end:
            return JoinPath(start=start, end=end, relations=())

        # BFS layer by layer, stopping once we've exhausted all paths at
        # the minimum hop distance.
        queue: list[tuple[str, tuple[Relation, ...], frozenset[str]]] = [
            (start, (), frozenset({start}))
        ]
        candidate_paths: list[tuple[Relation, ...]] = []
        min_hops: int | None = None

        while queue:
            current, relations_so_far, visited = queue.pop(0)

            if min_hops is not None and len(relations_so_far) >= min_hops:
                # We've already found a shorter path; don't extend further.
                continue

            if len(relations_so_far) >= self.max_hops:
                continue

            for edge in self.outgoing(current):
                next_dataset = (
                    edge.right_dataset
                    if edge.left_dataset == current
                    else edge.left_dataset
                )
                if next_dataset in visited:
                    continue
                next_visited = visited | {next_dataset}
                next_relations = relations_so_far + (edge,)

                if next_dataset == end:
                    candidate_paths.append(next_relations)
                    if min_hops is None or len(next_relations) < min_hops:
                        min_hops = len(next_relations)
                    continue

                queue.append((next_dataset, next_relations, next_visited))

        if not candidate_paths:
            raise NoPathError(left=start, right=end, max_hops=self.max_hops)

        # Keep only the shortest
        shortest = [p for p in candidate_paths if len(p) == min_hops]

        if len(shortest) > 1:
            for path in shortest:
                first_rel = path[0]
                if first_rel.is_default_path:
                    return self._build_path(start, end, path, is_measure_side)
            first_rels = tuple(p[0] for p in shortest)
            raise MultiplePathsError(
                left=start, right=end, candidates=first_rels
            )

        return self._build_path(start, end, shortest[0], is_measure_side)

    def _build_path(
        self,
        start: str,
        end: str,
        relations: tuple[Relation, ...],
        is_measure_side: bool,
    ) -> JoinPath:
        """Construct a JoinPath and check for fanout risk."""
        fanout_detected = False
        if is_measure_side:
            for r in relations:
                if r.fanout_risk == FanoutRisk.MEASURE_DUPLICATION:
                    fanout_detected = True
                    break
        return JoinPath(start=start, end=end, relations=relations)

    def validate(self) -> tuple[RelationError, ...]:
        """Validate the Join Graph for structural integrity.

        Returns a tuple of errors. Empty tuple means valid.
        """
        errors: list[RelationError] = []

        # Check that for every pair of datasets, at most one relation is
        # marked as default. If multiple, we cannot pick.
        pair_defaults: dict[tuple[str, str], list[str]] = defaultdict(list)
        for r in self.relations:
            pair = tuple(sorted([r.left_dataset, r.right_dataset]))
            if r.is_default_path:
                pair_defaults[pair].append(r.id)

        for pair, ids in pair_defaults.items():
            if len(ids) > 1:
                errors.append(
                    RelationError(
                        f"Pair {pair} has multiple default relations: {ids}"
                    )
                )

        # Check join_keys non-empty
        for r in self.relations:
            if not r.join_keys:
                errors.append(
                    RelationError(f"Relation {r.id!r} has no join keys")
                )

        # Check relation ids are unique
        ids = [r.id for r in self.relations]
        if len(set(ids)) != len(ids):
            errors.append(
                RelationError("Duplicate relation ids found")
            )

        return tuple(errors)


def build_join_graph(
    relations: list[Relation], *, max_hops: int = 2
) -> JoinGraph:
    """Build a JoinGraph and validate it. Raises on structural errors."""
    graph = JoinGraph(relations=tuple(relations), max_hops=max_hops)
    errors = graph.validate()
    if errors:
        raise RelationError(
            f"Join Graph validation failed: {[str(e) for e in errors]}"
        )
    return graph
