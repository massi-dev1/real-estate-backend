"""Lead-scoring seam (§8.18, §8.4).

Part 8 shipped a rules-based lead score computed inline in ``LeadsService``.
This part does **not** replace it — it factors the scoring *decision* out behind
a :class:`LeadScorer` protocol so a model-based scorer can be swapped in later
without touching the leads call site. The current rules implementation
(:class:`RulesLeadScorer`) already satisfies the protocol; no model training
happens in this part.

The scorer works over a provider-neutral :class:`LeadScoringFeatures` DTO built
by the leads service from a lead + its activities, so a future scorer never
imports leads' models — the same boundary-DTO stance as the portal adapter's
``PortalListing``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Score bounds (§8.4). A scorer clamps to this range.
MIN_SCORE = 0
MAX_SCORE = 100


@dataclass(frozen=True, slots=True)
class LeadScoringFeatures:
    """A provider-neutral feature vector for one lead (§8.4).

    Built by the leads service; no leads model type crosses this boundary, so a
    model-based scorer can consume it without importing ``modules/leads``.
    """

    # Per-source base weight, already resolved by the leads service (it owns the
    # source → weight table). Keeping the resolved number here — rather than the
    # raw source — means a swapped-in scorer doesn't need the leads enum.
    source_weight: int
    attached_to_listing: bool
    engagement_count: int
    days_since_last_activity: int
    no_show_count: int


@runtime_checkable
class LeadScorer(Protocol):
    """The contract every lead scorer satisfies (§8.4/§8.18). A model-based
    scorer implements the same method and is injected at the same call site."""

    def score(self, features: LeadScoringFeatures) -> int:
        """A 0-100 lead score for ``features``."""


# Weights of the engagement/recency/no-show signals. Kept here alongside the
# scorer so the rules live in one place; the source weights stay in the leads
# service (it owns the source enum). Values match Part 8 exactly — this is a
# pure extraction, not a behaviour change.
_ENGAGEMENT_POINTS = 5
_ENGAGEMENT_CAP = 25
_LISTING_BONUS = 15
_RECENCY_DECAY_CAP = 20
_NO_SHOW_PENALTY = 15


class RulesLeadScorer:
    """The Part 8 rules-based scorer, now behind the :class:`LeadScorer` seam.

    Deterministic and side-effect-free: source weight + listing-attach bonus +
    capped engagement minus recency decay minus no-show penalty, clamped 0-100.
    """

    def score(self, features: LeadScoringFeatures) -> int:
        score = features.source_weight
        # "Attached to a listing" is a coarse intent proxy (§8.4 — no budget
        # field exists on the fixed schema).
        score += _LISTING_BONUS if features.attached_to_listing else 0
        score += min(features.engagement_count * _ENGAGEMENT_POINTS, _ENGAGEMENT_CAP)
        score -= min(features.days_since_last_activity, _RECENCY_DECAY_CAP)
        score -= features.no_show_count * _NO_SHOW_PENALTY
        return max(MIN_SCORE, min(MAX_SCORE, score))


def build_lead_scorer() -> LeadScorer:
    """The active lead scorer. Rules-based today; a model-based scorer is
    selected here (by config) the day one exists — the leads call site is
    unaffected."""
    return RulesLeadScorer()
