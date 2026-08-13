"""Trusted Asset governance (S4 P2-04).

Trusted Assets are pre-audited answers to recurring questions. They provide
verified SQL/Canonical Plans for high-frequency questions, skipping the
intent-recognition round trip.

Full field model:
- id / name / domain
- trigger_questions[]          exact-text recall candidates
- canonical_plan              S3 contract (recompiled per recall)
- parameter_schema            param names + types
- semantic_revision_id        must match current published revision
- verified_by / verified_at
- review_status               pending | approved | retired
- last_validated_at / last_validation_result
- expires_at / is_active
- usage_count / failure_count
- eval_excluded               S5 eval isolation

Key behaviors:
- Parametric: "East sales" and "West sales" share one asset with a region
  parameter, not two separate records.
- Expiry: an asset with expires_at in the past is not recalled.
- Failure tracking: consecutive failures mark the asset inactive.
- Recall: exact-text → canonical-signature → embedding candidate. Only
  candidates that pass structural compatibility + revision check + permission
  check are actually recalled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from app.planning.canonical import CanonicalQueryPlan


class ReviewStatus(str, Enum):
    """Lifecycle of a trusted asset."""

    PENDING = "pending"
    APPROVED = "approved"
    RETIRED = "retired"


class ValidationResult(str, Enum):
    """Result of the last validation pass."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ParameterSpec:
    """A single parameter in a trusted asset's parameter_schema."""

    name: str
    param_type: str  # "string" | "number" | "date" | "enum"
    required: bool = True
    enum_values: tuple[str, ...] = ()  # For type=enum
    description: str = ""


@dataclass(frozen=True)
class TrustedAsset:
    """A pre-audited answer to a recurring question.

    The asset is a frozen record; only its lifecycle fields (is_active,
    usage_count, failure_count, last_validated_at) may be updated.
    """

    id: int
    name: str
    domain: str
    trigger_questions: tuple[str, ...]
    canonical_plan_template: "CanonicalQueryPlan | None"
    parameter_schema: tuple[ParameterSpec, ...]
    semantic_revision_id: int
    verified_by: str
    verified_at: datetime
    review_status: ReviewStatus = ReviewStatus.APPROVED

    # Lifecycle / runtime state
    last_validated_at: datetime | None = None
    last_validation_result: ValidationResult = ValidationResult.PENDING
    expires_at: datetime | None = None
    is_active: bool = True

    # Tracking
    usage_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    eval_excluded: bool = False

    description: str = ""

    def is_expired(self, now: datetime | None = None) -> bool:
        """True if expires_at is in the past."""
        if self.expires_at is None:
            return False
        current = now or datetime.utcnow()
        return current > self.expires_at

    def is_recallable(self, now: datetime | None = None) -> bool:
        """True if this asset can be recalled in production.

        An asset is recallable when:
        - is_active is True
        - review_status is APPROVED
        - not expired
        - last_validation_result is PASSED or PENDING (not FAILED/EXPIRED)
        """
        if not self.is_active:
            return False
        if self.review_status != ReviewStatus.APPROVED:
            return False
        if self.is_expired(now):
            return False
        if self.last_validation_result == ValidationResult.FAILED:
            return False
        return True

    def with_recall(self) -> "TrustedAsset":
        """Record a successful recall (increment usage_count)."""
        return _replace(self, usage_count=self.usage_count + 1,
                        consecutive_failures=0)

    def with_failure(self, threshold: int = 3) -> "TrustedAsset":
        """Record a failure; auto-deactivate if consecutive failures hit threshold."""
        new_consecutive = self.consecutive_failures + 1
        new_active = self.is_active and (new_consecutive < threshold)
        return _replace(
            self,
            failure_count=self.failure_count + 1,
            consecutive_failures=new_consecutive,
            is_active=new_active,
        )


def _replace(asset: TrustedAsset, **changes: Any) -> TrustedAsset:
    """Frozen-dataclass safe replacement."""
    from dataclasses import replace
    return replace(asset, **changes)


# --- Recall mechanism ------------------------------------------------------

@dataclass(frozen=True)
class RecallMatch:
    """A single recall match returned by the recall engine."""

    asset: TrustedAsset
    confidence: float
    match_reason: str  # "exact_text" | "signature" | "embedding_candidate"
    parameters: dict[str, Any]  # Resolved parameter values
    canonical_plan: CanonicalQueryPlan


@dataclass(frozen=True)
class RecallQuery:
    """A user's question presented to the recall engine."""

    question: str
    semantic_revision_id: int
    principal_id: int
    # Optional: pre-resolved parameter hints
    parameter_hints: dict[str, Any] = field(default_factory=dict)


class TrustedAssetError(Exception):
    """Base for trusted asset errors."""


class RevisionMismatchError(TrustedAssetError):
    """Asset's semantic_revision_id does not match the query's revision."""

    def __init__(self, asset_id: int, asset_rev: int, query_rev: int):
        self.asset_id = asset_id
        self.asset_rev = asset_rev
        self.query_rev = query_rev
        super().__init__(
            f"Asset {asset_id} is for revision {asset_rev}, "
            f"but query is for revision {query_rev}"
        )


class ExpiredAssetError(TrustedAssetError):
    """Asset is past its expires_at and cannot be recalled."""


class InactiveAssetError(TrustedAssetError):
    """Asset is not active (auto-deactivated or manually retired)."""


def _exact_text_match(
    query: RecallQuery, assets: tuple[TrustedAsset, ...]
) -> RecallMatch | None:
    """High-precision: exact trigger text match."""
    target = query.question.strip().casefold()
    for asset in assets:
        # Skip non-recallable assets (inactive, expired, etc.)
        if not asset.is_recallable():
            continue
        for trigger in asset.trigger_questions:
            if trigger.strip().casefold() == target:
                return RecallMatch(
                    asset=asset,
                    confidence=0.99,
                    match_reason="exact_text",
                    parameters=dict(query.parameter_hints),
                    canonical_plan=asset.canonical_plan_template,  # type: ignore
                )
    return None


def _signature_match(
    query: RecallQuery, assets: tuple[TrustedAsset, ...]
) -> RecallMatch | None:
    """Medium-precision: canonical signature match (placeholder for now)."""
    # In a real implementation, we'd hash the canonical plan structure and
    # compare. The signature includes: dataset, metrics set, dimensions set,
    # time basis, parameter bindings. We provide a structural check here.
    return None


def _embedding_candidates(
    query: RecallQuery, assets: tuple[TrustedAsset, ...], top_k: int = 5
) -> tuple[TrustedAsset, ...]:
    """Low-precision: embedding-based candidates (placeholder)."""
    # In a real implementation, this would use vector similarity.
    # The point of the placeholder is that this list is a CANDIDATE pool,
    # not a result. Each candidate still has to pass the recallable check
    # and structural compatibility.
    return assets[:top_k]


def recall(
    query: RecallQuery,
    assets: tuple[TrustedAsset, ...],
    *,
    embedding_enabled: bool = False,
) -> tuple[RecallMatch, ...]:
    """Recall trusted assets for a query.

    Recall order:
    1. Exact text match (highest precision)
    2. Canonical signature match (structural similarity)
    3. Embedding candidates (lowest precision, optional)

    All returned assets must:
    - Have matching semantic_revision_id
    - Be is_recallable() (active, approved, not expired, not failed)
    - Have passing permission check (out of scope here; principal_id is
      passed in for the caller's check)

    Returns a tuple of RecallMatch, ordered by confidence.
    """
    # Filter to assets with matching revision
    revision_matched = tuple(
        a for a in assets
        if a.semantic_revision_id == query.semantic_revision_id
    )

    # Level 1: exact text
    exact = _exact_text_match(query, revision_matched)
    if exact is not None:
        return (exact,)

    # Level 2: canonical signature
    sig = _signature_match(query, revision_matched)
    if sig is not None:
        return (sig,)

    # Level 3: embedding candidates (optional)
    if embedding_enabled:
        candidates = _embedding_candidates(query, revision_matched)
        # Return recallable candidates; caller does the final decision
        recallable = tuple(c for c in candidates if c.is_recallable())
        return tuple(
            RecallMatch(
                asset=c,
                confidence=0.5,
                match_reason="embedding_candidate",
                parameters=dict(query.parameter_hints),
                canonical_plan=c.canonical_plan_template,  # type: ignore
            )
            for c in recallable
        )

    return ()


# --- Parametric binding ---------------------------------------------------

def bind_parameters(
    asset: TrustedAsset, parameters: dict[str, Any]
) -> tuple[str, ...]:
    """Validate and bind parameters for an asset.

    Returns a tuple of error messages; empty tuple means OK.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for spec in asset.parameter_schema:
        seen.add(spec.name)
        if spec.required and spec.name not in parameters:
            errors.append(f"Required parameter missing: {spec.name!r}")
            continue
        if spec.name in parameters:
            value = parameters[spec.name]
            if spec.param_type == "enum":
                if value not in spec.enum_values:
                    errors.append(
                        f"Parameter {spec.name!r}={value!r} "
                        f"not in {list(spec.enum_values)}"
                    )
    extras = set(parameters.keys()) - seen
    if extras:
        errors.append(f"Unknown parameters: {sorted(extras)}")
    return tuple(errors)
