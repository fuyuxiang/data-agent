"""Join Graph for multi-dataset queries (S4 P2-01).

Relations form a static, audited Join Graph. LLM never composes a join
on the fly — it picks from a predefined set of relations.

Each Relation describes how two datasets connect:

- left_dataset / right_dataset
- join_keys[] — column pairs (left.column, right.column)
- cardinality — one_to_one | one_to_many | many_to_one | many_to_many
- optionality — inner | left
- fanout_risk — none | measure_duplication
- allowed_directions[] — which side is the "from" side
- is_default_path — preferred when multiple paths exist

The compiler uses JoinGraph.find_path() to resolve metric/dimension
references into a sequence of Relations. If the graph has multiple paths
and no default, the query is rejected — we never guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Cardinality(str, Enum):
    """How the two sides of a join relate."""

    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class Optionality(str, Enum):
    """Whether unmatched rows are kept."""

    INNER = "inner"
    LEFT = "left"


class FanoutRisk(str, Enum):
    """Whether the relation can multiply row counts on aggregation."""

    NONE = "none"
    MEASURE_DUPLICATION = "measure_duplication"


@dataclass(frozen=True)
class JoinKey:
    """A column pair used in a join."""

    left_column: str
    right_column: str


@dataclass(frozen=True)
class Relation:
    """A pre-audited edge in the Join Graph.

    `is_default_path` is consulted when multiple Relations between the same
    two datasets exist: callers must either mark one as default or specify
    the path explicitly. We never guess.
    """

    id: str
    left_dataset: str
    right_dataset: str
    join_keys: tuple[JoinKey, ...]
    cardinality: Cardinality
    optionality: Optionality = Optionality.INNER
    fanout_risk: FanoutRisk = FanoutRisk.NONE
    allowed_directions: tuple[str, ...] = ("left_to_right",)
    is_default_path: bool = True
    description: str = ""


class RelationError(Exception):
    """Raised on Join Graph errors (e.g. multiple paths, missing relations)."""


class MultiplePathsError(RelationError):
    """Multiple relations between the same datasets with no default."""

    def __init__(
        self,
        left: str,
        right: str,
        candidates: tuple[Relation, ...],
    ):
        self.left = left
        self.right = right
        self.candidates = candidates
        names = ", ".join(r.id for r in candidates)
        super().__init__(
            f"Multiple relations between {left!r} and {right!r}: {names}. "
            f"Mark one as is_default_path=True or specify path explicitly."
        )


class NoPathError(RelationError):
    """No relation between two datasets in the Join Graph."""

    def __init__(self, left: str, right: str, max_hops: int):
        self.left = left
        self.right = right
        self.max_hops = max_hops
        super().__init__(
            f"No path from {left!r} to {right!r} within {max_hops} hops"
        )
