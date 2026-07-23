"""Plan → quota-limits table (§8.16), code-owned like the RBAC matrix.

Kept in code (not the DB) so it is auditable in git and testable. A quota is
``None`` when unlimited. Write-time enforcement lives in the owning services
(listing create checks ``max_listings``, agent-profile create ``max_agents``,
media confirm ``storage_gb``); this module only defines the numbers and the
lookup, so there is one source of truth for "what does plan X allow".
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlanLimits:
    """Per-plan quota ceilings. ``None`` means unlimited for that dimension."""

    key: str
    name: str
    max_listings: int | None
    max_agents: int | None
    storage_gb: int | None
    monthly_emails: int | None


# Ordered loosely by tier. ``trial`` is what a new tenant starts on (matching
# the default ``tenants.plan``); an over-quota action on any plan is a 403
# ``quota-exceeded`` problem+json (§9).
PLANS: dict[str, PlanLimits] = {
    "trial": PlanLimits(
        key="trial",
        name="Trial",
        max_listings=25,
        max_agents=3,
        storage_gb=1,
        monthly_emails=500,
    ),
    "starter": PlanLimits(
        key="starter",
        name="Starter",
        max_listings=100,
        max_agents=10,
        storage_gb=10,
        monthly_emails=5_000,
    ),
    "growth": PlanLimits(
        key="growth",
        name="Growth",
        max_listings=1_000,
        max_agents=50,
        storage_gb=100,
        monthly_emails=50_000,
    ),
    "enterprise": PlanLimits(
        key="enterprise",
        name="Enterprise",
        max_listings=None,
        max_agents=None,
        storage_gb=None,
        monthly_emails=None,
    ),
}

DEFAULT_PLAN = "trial"


def plan_limits(plan: str) -> PlanLimits:
    """Limits for ``plan``, falling back to the trial tier for an unknown value
    (a stale/misconfigured plan string must not crash a write path — it degrades
    to the most restrictive tier, which is the safe direction)."""
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


def storage_bytes_limit(plan: str) -> int | None:
    gb = plan_limits(plan).storage_gb
    return None if gb is None else gb * 1024 * 1024 * 1024
