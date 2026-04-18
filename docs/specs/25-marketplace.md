# 25 — Marketplace (deferred)

> **Not in v1.** This section reserves the design space and the
> seams so implementing it later is additive, not a migration of
> live data. The deployment ships with the marketplace **off**; the
> spec exists so v1 decisions in §02, §05, §11, §15, and §22 do not
> quietly foreclose the feature.

A deployment-wide layer on top of §22 where **agencies advertise
services** with a geographic area they will intervene in, and
**clients request services** for a specific place of intervention.
The platform takes a configurable **% fee** on amounts subsequently
billed between the two sides through the existing §22 work-order /
vendor-invoice / shift-billing plumbing.

## Design summary

- **One marketplace per deployment.** Entities live in the
  deployment scope (§05 "Deployment scope"), not inside a single
  workspace, so listings and requests are discoverable across
  every workspace on that deployment. The managed SaaS at
  `crew.day` is the intended host; self-hosted single-workspace
  deployments normally leave it off.
- **Auth-required.** Both agencies and clients are first-class
  crew.day workspaces before they touch the marketplace; there is
  no anonymous-lead path. A client who doesn't yet have a
  workspace walks the existing self-serve signup flow (§03) first.
  This keeps the auth, rate-limit, abuse, and PII model unchanged.
- **Discovery layer only.** When a match is accepted the system
  materialises an existing §22 `property_workspace_invite`
  (client-as-inviter → agency-as-recipient) and thereafter the
  normal §22 flow runs — `property_workspace` link,
  `work_order`, `quote`, `vendor_invoice`, `shift_billing`.
  There is no parallel dispatch pipeline.
- **Fee capture via append-only ledger.** A new
  `platform_fee_event` table records every fee accrual keyed to
  the marketplace match and to the billable source row
  (`shift_billing` or `vendor_invoice`). No fee columns on
  invoices, quotes, or work orders. The fee rate is **snapshotted
  onto the match** at match-time so later deployment-wide rate
  changes never rewrite history.
- **Capability-gated.** A deployment setting
  `settings.marketplace_enabled` (bool, default `false`, §01
  "Capability registry") toggles the surface. Off = the routes,
  UI, and cron workers are absent; rows are not written. The
  managed SaaS enables it; self-host leaves it off unless the
  operator wants to run their own marketplace.
- **crew.day does not move money.** As with §22 vendor invoices
  and payroll, the fee ledger records **what is owed to the
  platform**; collection and reconciliation happen externally.
  Payment execution is a post-v1 concern (§00 N1, N3; §19 Beyond
  v1).

## Entities (shape)

All rows carry a deployment-scope id (the synthetic deployment
row described in §05 "Deployment scope"). They are **not**
`workspace_id`-scoped; instead they reference workspaces by FK.

### `marketplace_listing`

An agency's advertisement.

| field                    | notes                                                                             |
|--------------------------|-----------------------------------------------------------------------------------|
| id                       | ULID PK                                                                           |
| agency_workspace_id      | FK → `workspace.id`; the workspace publishing the ad                              |
| title                    | short display name ("CleanCo — Côte d'Azur turnovers")                            |
| description_md           | long-form copy                                                                    |
| work_role_keys           | array of `work_role.key` offered (maid, driver, gardener, …); §05                 |
| service_area_geojson     | RFC 7946 GeoJSON `Feature` / `FeatureCollection` with a Polygon / MultiPolygon geometry; the area the agency will intervene in |
| currency                 | ISO 4217 — the currency of advertised rates                                       |
| rate_band_cents          | optional `{low, high}` hourly advertised rates, per work role                     |
| contact_surface_json     | how clients get in touch — usually routed through the in-app messaging seam (§10, §23); the listing never exposes the agency's email / phone directly |
| status                   | `draft \| published \| paused \| archived`                                        |
| published_at / archived_at | lifecycle timestamps                                                            |
| created_at / updated_at  |                                                                                   |

### `service_request`

A client's request for service.

| field                      | notes                                                                           |
|----------------------------|---------------------------------------------------------------------------------|
| id                         | ULID PK                                                                         |
| client_workspace_id        | FK → `workspace.id`; the posting workspace                                      |
| property_id                | FK → `property.id`; the place of intervention (§04)                             |
| work_role_key              | the work the client needs (maid, handyman, …)                                   |
| description_md             | "replace pool pump seal" / "weekly cleanings through summer"                    |
| schedule_hint_json         | free-form: `{kind: one_off, on: date}` or `{kind: recurring, rrule: ..., ...}`  |
| budget_band_cents          | optional `{low, high}` budget guidance                                          |
| currency                   | ISO 4217                                                                        |
| location_geojson           | RFC 7946 GeoJSON `Feature` with a Point geometry (derived from `property.address_json` + lat/lng; stored on the request so matches don't leak post-match property edits) |
| status                     | `open \| proposed \| matched \| fulfilled \| cancelled`                         |
| expires_at                 | optional auto-close                                                             |
| created_at / updated_at    |                                                                                 |

### `marketplace_match`

An accepted pairing of a listing with a request — the durable
handle the fee ledger references.

| field                       | notes                                                                                             |
|-----------------------------|---------------------------------------------------------------------------------------------------|
| id                          | ULID PK                                                                                           |
| listing_id                  | FK → `marketplace_listing.id`                                                                     |
| request_id                  | FK → `service_request.id`                                                                         |
| agency_workspace_id         | denormalised for reporting                                                                        |
| client_workspace_id         | denormalised for reporting                                                                        |
| property_id                 | denormalised for reporting                                                                        |
| platform_fee_bps            | basis points (e.g. `1000` = 10 %). **Snapshot** of `settings.platform_fee_default_bps` at match time; per-match override is possible but not authored in v1 |
| fee_currency_policy         | `match_source` (fee is struck in the currency of each billable row) vs `fixed_<ISO>` (fee struck in one currency and converted). v1 default: `match_source` |
| proposed_by_user_id         | who clicked "accept" first                                                                        |
| accepted_by_user_id         | who confirmed on the other side                                                                   |
| accepted_at                 |                                                                                                   |
| source_invite_id            | FK → `property_workspace_invite.id`; the invite the match auto-creates. §22 §19                    |
| status                      | `pending \| accepted \| cancelled \| ended`                                                       |
| ended_at                    | match is "ended" when the underlying `property_workspace` link is revoked (§22); new matches can be opened thereafter |

Acceptance of a `marketplace_match` is an
**unconditionally approval-gated action** (§11) on both sides — an
agent may propose, a human commits. The same gate also fires for
the auto-generated `property_workspace_invite` (already gated in
§22).

### `platform_fee_event`

Append-only ledger of fee accruals.

| field                 | notes                                                                                    |
|-----------------------|------------------------------------------------------------------------------------------|
| id                    | ULID PK                                                                                  |
| match_id              | FK → `marketplace_match.id`                                                              |
| source_kind           | enum: `shift_billing \| vendor_invoice`                                                  |
| source_id             | ULID of the source row (`shift_billing.id` or `vendor_invoice.id`)                       |
| source_workspace_id   | FK → `workspace.id` — which workspace generated the billable row; one of the match sides |
| base_currency         | ISO 4217 — the currency of the billable row                                              |
| base_amount_cents     | what was billed between the two parties on this row                                      |
| fee_bps               | snapshot from the match at the moment this ledger row was written                        |
| fee_currency          | ISO 4217 — usually = `base_currency` under `match_source` policy                         |
| fee_amount_cents      | `base_amount_cents * fee_bps / 10_000`, rounded per §09 rounding rule                    |
| settled_at            | optional; set when the platform reconciles the fee out of band                           |
| created_at            |                                                                                          |

Rows are **never updated**. A reversal is a new row with negative
amounts and a `reason` pointer to the original — parallel to the
expense-claim / shift correction pattern in §09. This keeps the
ledger clean for a future collection pipeline.

## How it plugs into §22

When a `marketplace_match` transitions to `accepted`:

1. The marketplace layer calls the existing §22
   `property_workspace_invite.create` action — the client workspace
   is the inviter, the agency workspace is the recipient,
   `property_id` is `service_request.property_id`, proposed
   membership role is `managed_workspace`,
   `initial_share_settings_json` is whatever the client offered in
   the match acceptance card. The invite row carries a
   `source_match_id` forward reference (the invite migration in
   the marketplace milestone adds the nullable FK — the v1
   invite table from §22 does not carry it).
2. The agency workspace's owners accept or reject via the normal
   §22 invite flow. Rejection flips the match back to `pending`.
3. On acceptance the `property_workspace` link is live and every
   §22 flow runs exactly as today: `work_order`, `quote`,
   `vendor_invoice`, `shift_billing`.

The marketplace adds **one async worker** — `accrue_platform_fees`
— that watches `shift_billing.resolved` and
`vendor_invoice.approved` events (§10 webhook catalog). When the
event's property+agency pair belongs to an active
`marketplace_match`, the worker writes one `platform_fee_event`
row. Nothing else in §22 changes.

## Privacy and cross-workspace boundary

- Listings are **publishable**: their `title`, `description_md`,
  `service_area_geojson`, `work_role_keys`, and anonymised rate
  bands are visible to every authenticated workspace on the
  deployment. The agency's workspace name is visible; member
  names, addresses, and any PII inside property rows are not.
- Service requests are **semi-public**: the `location_geojson` is
  a single point (derived from `property.address_json`), and the
  property's name and address are hidden until a match is
  proposed. Agencies see "a client in Antibes wants a weekly
  turnover" but not which villa.
- On match acceptance the existing §15 "Cross-workspace
  visibility" boundary kicks in — same redactions as any other
  `property_workspace` link. Guest-identity widening still
  requires an explicit `share_guest_identity` flag on the invite.
- Listings and requests are subject to the workspace's plan +
  quota seam (§00 G13): free-tier caps on open listings per
  agency and open requests per client live in
  `workspace.quota_json` alongside the existing counters.

## Geometry storage

GeoJSON is stored as a JSONB column on Postgres and as TEXT (JSON
by convention) on SQLite. Queries in v1 of the feature (post-v1 of
the app) are done in application code — point-in-polygon with a
Shapely-style library is adequate for the expected data volumes.
Spatial indexing is reserved behind a new capability:

- `features.postgis` — set when the deployment is Postgres and
  the PostGIS extension is installed. When on, migrations add a
  `geography(Polygon, 4326)` column mirroring
  `service_area_geojson` plus a GIST index; query paths switch
  transparently. When off, the JSON column is the sole source of
  truth. Parallel to `features.fulltext_search` (§01
  "Capability registry").

No GIS-dependent behaviour ships in v1 of the app. The capability
seam exists so the later migration is additive, not destructive.

## Platform fee controls

Deployment-scope settings (read through the same capability
registry interface as everything else in §01):

| key                                  | type | default | notes                                                  |
|--------------------------------------|------|---------|--------------------------------------------------------|
| `settings.marketplace_enabled`       | bool | `false` | master kill-switch; when false the routes 404          |
| `settings.platform_fee_default_bps`  | int  | `1000`  | default 10 % snapshotted onto each new match           |
| `settings.platform_fee_currency_policy` | enum | `match_source` | `match_source \| fixed_<ISO>`                      |

Operator sets them via `crewday deploy settings set ...` (§13).
None of these live in v1's `deployment_setting` table seed — they
are added in the marketplace migration.

## Actions (reserved)

Added to §05's action catalog **only** when the marketplace ships.
Every mutation is workspace-scoped to the inviter or the listing
owner and authored by an `owners` or `managers` member on that
workspace. Money-adjacent actions are unconditionally
approval-gated.

| action                                   | gate                             |
|------------------------------------------|----------------------------------|
| `marketplace_listing.create / update / archive` | normal permission rule   |
| `service_request.create / update / cancel`      | normal permission rule   |
| `marketplace_match.propose`                     | normal permission rule   |
| `marketplace_match.accept`                      | **unconditionally gated** |
| `marketplace_match.cancel`                      | normal permission rule   |
| `platform_fee_event.settle` (operator-side)     | deployment-scope `owners` |

## Out of scope (deferred forever in v1; listed so v1 doesn't drift into them)

- Routes, SSE channels, chat-agent tools, CLI verbs, mock pages,
  fixtures, and OpenAPI entries for any of the above.
- Marketplace-scope search / ranking / reviews.
- Automated dispute resolution.
- Automated collection of platform fees from either side — the
  ledger is recorded; moving money is a separate initiative, same
  rule as payroll and vendor invoices.
- Multi-currency fee conversion (beyond the `match_source` policy
  already reserved).
- Tax / VAT handling on platform fees.
- A public (pre-auth) browse surface. Every read is
  session-authenticated.

## v1 seams reserved

The only v1-visible hooks that exist to keep this feature
additive are:

- **Deployment scope** (§05) is already live in v1 for LLM
  config; the marketplace reuses that scope for its entities.
- **`property_workspace_invite`** (§22) already models the
  bilateral sharing flow. The marketplace materialises an invite
  rather than inventing a second path. The `source_match_id`
  column is added by the marketplace migration, not in v1.
- **Plan + quota seam** (§00 G13, §02 `workspace.quota_json`) can
  hold future per-workspace listing and request limits without
  schema churn.
- **Capability registry** (§01) already hosts `features.*` and
  `settings.*` booleans; `features.postgis` and
  `settings.marketplace_*` slot in without runtime code change.
- **Webhook catalog** (§10) already carries
  `shift_billing.resolved` and `vendor_invoice.approved`, which
  are the hooks the later `accrue_platform_fees` worker
  subscribes to.

No v1 table gains a marketplace column. No v1 route path is
reserved. When the feature ships, its migration is strictly
additive.
