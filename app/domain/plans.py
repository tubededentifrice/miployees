"""Plan-tier quota defaults used when seeding a new workspace.

Today this module is a small seam so self-serve signup (cd-3i5) can
pick a deterministic quota payload without hard-coding numbers inside
the domain service. The numbers below are **defaults** — the full plan
catalogue (paid tiers, enterprise overrides, per-tier feature flags)
lives in §02 `Plan + quota` and lands with cd-055's abuse caps.

Until the full plan catalogue lands, treat this file as the single
source of truth for the free-tier starting values and the "tight
initial caps" (§03 "Self-serve signup" → "Tight initial caps") the
signup flow applies — 10 % of the free-tier ceiling, lifted once the
workspace reaches ``verification_state='human_verified'``.

The numbers are intentionally conservative placeholders; reconcile
with §02 once the plan catalogue spec lands.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "FREE_TIER_DEFAULTS",
    "seed_free_tier_10pct",
    "seed_free_tier_quota",
    "tight_cap_cents",
]


# Free-tier ceilings. These are the "full" free-plan caps; the signup
# flow multiplies each by :data:`_TIGHT_CAP_FRACTION` until the
# workspace passes human verification.
#
# * ``storage_bytes`` — total object-storage budget (5 GiB). Upload
#   quotas (§15 ``upload_bytes_max``) layer on top.
# * ``users_max`` — seats on the workspace.
# * ``properties_max`` — distinct property rows.
# * ``llm_budget_cents_30d`` — rolling 30-day LLM spend cap in US cents
#   (§11). Matches the :class:`~app.capabilities.DeploymentSettings`
#   default so a brand-new deployment and a brand-new workspace start
#   with the same ceiling until an operator raises either.
FREE_TIER_DEFAULTS: Final[dict[str, int]] = {
    "storage_bytes": 5 * 1024 * 1024 * 1024,  # 5 GiB
    "users_max": 10,
    "properties_max": 3,
    "llm_budget_cents_30d": 500,  # $5.00
}


# Fraction applied during the "tight initial caps" window (§03).
# 10 % of every integer ceiling. We ``max(..., 1)`` so a rounding fall
# to zero doesn't silently zero out a cap — a worker seat of 0 would
# refuse the first invite the workspace ever tries to send.
_TIGHT_CAP_FRACTION: Final[float] = 0.10


def seed_free_tier_quota() -> dict[str, int]:
    """Return a fresh copy of the full free-tier quota defaults.

    Returned as a mutable dict so the caller can land it in
    :attr:`app.adapters.db.workspace.models.Workspace.quota_json`
    without aliasing the module-level constant.
    """
    return dict(FREE_TIER_DEFAULTS)


def seed_free_tier_10pct() -> dict[str, int]:
    """Return the **tight initial** quota blob — 10 % of every default.

    Seeds ``workspace.quota_json`` for a freshly minted workspace at
    signup. The caps lift to :data:`FREE_TIER_DEFAULTS` automatically
    once the workspace reaches ``verification_state='human_verified'``
    (§03 "Tight initial caps"). Integer ceilings are floored at 1 so a
    rounding fall to zero never silently locks out a workspace.
    """
    tightened: dict[str, int] = {}
    for key, value in FREE_TIER_DEFAULTS.items():
        scaled = int(value * _TIGHT_CAP_FRACTION)
        tightened[key] = max(scaled, 1)
    return tightened


def tight_cap_cents(full_cap_cents: int) -> int:
    """Return the tight-cap value (§03) for a given full-tier ceiling.

    Used by the signup flow to scale an operator-overridden LLM budget
    cap (e.g. :attr:`app.capabilities.DeploymentSettings.
    llm_default_budget_cents_30d`) by the same 10 % fraction
    :func:`seed_free_tier_10pct` applies to ``workspace.quota_json``,
    so the ledger row (§11 "Cap") and the quota blob always agree on
    the number the workspace runs at until it reaches
    ``verification_state='human_verified'``.

    ``full_cap_cents = 0`` round-trips to ``0`` — a deployment that
    hard-disables LLMs by setting the full-tier cap to zero keeps the
    tight cap at zero too, rather than silently promoting it to 1
    cent (which would ostensibly allow ~one unpriced call through).
    Non-zero values floor at 1 cent so a sub-threshold override
    doesn't silently lock the workspace out of every LLM call — this
    matches the :func:`seed_free_tier_10pct` convention for the
    other quota knobs.
    """
    if full_cap_cents <= 0:
        return 0
    scaled = int(full_cap_cents * _TIGHT_CAP_FRACTION)
    return max(scaled, 1)
