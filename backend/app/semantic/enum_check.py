"""Enum alias conflict detection (S4 P1-08).

Problem: resolve_enum returns the first match. Two enum values sharing an
alias (e.g. both "华东" and "EAST" mapping to different physical values)
silently pick whichever comes first in the tuple — the wrong entity.

Fix:
- Lint at publish time: NFKC + casefold + whitespace-stripped aliases
  must be unique per field.
- resolve_enum returns ALL candidates, not just the first. Callers must
  raise a clarification when there are 0 or N>1 candidates.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from app.semantic.model import DatasetDef, EnumValueDef, FieldDef


@dataclass(frozen=True)
class EnumAliasConflict:
    """A single alias conflict within one field."""

    field_name: str
    alias_normalized: str
    # The enum values that share this alias; each entry is the EnumValueDef.
    competing_values: tuple[EnumValueDef, ...]

    def describe(self) -> str:
        values_str = ", ".join(
            f"physical={v.physical_value!r} business={v.business_value!r}"
            for v in self.competing_values
        )
        return (
            f"Field {self.field_name!r} has alias {self.alias_normalized!r} "
            f"shared by: {values_str}"
        )


def _normalize_alias(alias: str) -> str:
    """Normalize an alias for comparison.

    Steps: NFKC unicode normalisation, casefold (lowercase + locale-aware),
    then strip all whitespace. The result is a canonical key under which
    aliases must be unique.
    """
    normalized = unicodedata.normalize("NFKC", alias)
    normalized = normalized.casefold()
    normalized = "".join(c for c in normalized if not c.isspace())
    return normalized


def find_alias_conflicts(dataset: DatasetDef) -> tuple[EnumAliasConflict, ...]:
    """Find alias conflicts across all enum fields in a dataset.

    Conflicts arise when two enum values share a normalised alias — either
    the same alias string, a normalised equivalent, or the same business
    value. physical_value is the unique key and is not part of the alias pool.

    Each conflicting alias is reported once with all competing values.
    Returns a tuple of EnumAliasConflict, empty if no conflicts.
    """
    conflicts: list[EnumAliasConflict] = []

    for field in dataset.fields:
        if not field.enum_values:
            continue
        # Map: normalised alias -> list of EnumValueDef
        alias_to_values: dict[str, list[EnumValueDef]] = defaultdict(list)

        for enum_value in field.enum_values:
            # An enum value contributes its business_value + aliases to the
            # candidate pool. physical_value is the unique key and excluded.
            candidates = (enum_value.aliases + (enum_value.business_value,))
            for candidate in candidates:
                if not candidate:
                    continue
                normalised = _normalize_alias(candidate)
                if normalised:
                    alias_to_values[normalised].append(enum_value)

        # Detect conflicts: any alias with more than one value.
        for alias_normalized, values in alias_to_values.items():
            if len(values) > 1:
                conflicts.append(
                    EnumAliasConflict(
                        field_name=field.name,
                        alias_normalized=alias_normalized,
                        competing_values=tuple(values),
                    )
                )

    return tuple(conflicts)


def assert_no_alias_conflicts(dataset: DatasetDef) -> None:
    """Raise if the dataset has any alias conflicts.

    Called from the publish gate; should never run on an unvalidated draft.
    """
    conflicts = find_alias_conflicts(dataset)
    if conflicts:
        descriptions = "\n".join(c.describe() for c in conflicts)
        raise EnumConflictError(
            f"Dataset {dataset.name!r} has {len(conflicts)} alias conflict(s):\n"
            f"{descriptions}"
        )


class EnumConflictError(Exception):
    """Raised when one or more enum alias conflicts are detected."""


# --- Multi-candidate resolution --------------------------------------------

@dataclass(frozen=True)
class EnumResolution:
    """Result of resolving a spoken value to enum candidates.

    A resolution carries every physical_value that matches the input,
    not just the first. Callers MUST inspect candidates and act on
    - 0 candidates: raise clarification
    - 1 candidate: use it
    - N > 1: must raise clarification (ambiguous)
    """

    spoken: str
    candidates: tuple[EnumValueDef, ...]

    @property
    def is_unique(self) -> bool:
        return len(self.candidates) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def is_empty(self) -> bool:
        return len(self.candidates) == 0

    def unique_value(self) -> str | None:
        """Return the single physical value if unique, else None."""
        if len(self.candidates) == 1:
            return self.candidates[0].physical_value
        return None


def resolve_enum_all(
    dataset: DatasetDef,
    field_name: str,
    spoken: str,
) -> EnumResolution:
    """Resolve a spoken value to all matching enum candidates.

    Returns 0, 1, or N candidates; never silently picks one. Match is by
    business_value or alias; physical_value is the resolved output, not a
    match key.
    """
    if not dataset.has_field(field_name):
        return EnumResolution(spoken=spoken, candidates=())

    field = dataset.field(field_name)
    if not field.enum_values:
        return EnumResolution(spoken=spoken, candidates=())

    target = _normalize_alias(spoken)
    if not target:
        return EnumResolution(spoken=spoken, candidates=())

    matches: list[EnumValueDef] = []
    for value in field.enum_values:
        candidates = (value.aliases + (value.business_value,))
        for candidate in candidates:
            if not candidate:
                continue
            if _normalize_alias(candidate) == target:
                matches.append(value)
                break  # one match per value is enough

    return EnumResolution(spoken=spoken, candidates=tuple(matches))
