# 04 — Properties, areas, stays, guests

## Property

A property is a physical place the workspace operates in. The primary
residence, a vacation home, an STR unit, or a yacht all count. Every
property contains one or more **units** (§"Unit" below); stays, iCal
feeds, and lifecycle bundles are scoped to units, not directly to the
property.

### Fields

| field                 | type        | notes                          |
|-----------------------|-------------|--------------------------------|
| id                    | ULID PK     |                                |
| workspace_id          | ULID FK     |                                |
| name                  | text        | "Villa Sud", "Apt 3B"          |
| kind                  | enum        | `residence | vacation | str | mixed` (behavior below) |
| address_json          | jsonb/text  | structured address             |
| timezone              | text        | IANA (`Europe/Paris`)          |
| default_currency      | text        | ISO 4217, inherits workspace   |
| client_org_id         | ULID FK?    | `organization.id` (§22). Null = workspace-owned / self-managed. Set = billable to that client; tasks, shifts, work_orders at this property carry the client forward for rate resolution and CSV rollup. Referenced org must have `is_client = true`. |
| owner_user_id         | ULID FK?    | `users.id`. Optional "owner of record" pointer — the natural person who owns the property in real life (e.g. the homeowner whose villa an agency manages). Display-only: authorisation is governed by the property's `owner_workspace` membership and that workspace's `owners` permission group, never by this column. Useful in the agency / multi-belonging case (§02) so the UI can show "Villa du Lac · owner: Vincent Dupont" alongside the workspace memberships. |
| country               | text        | ISO-3166-1 alpha-2. Required. Authoritative source: `address_json.country` when present; otherwise inherits workspace `default_country`. Drives holiday suggestions, payslip jurisdiction, locale derivation. |
| locale                | text?       | BCP-47. Nullable; when null, derived from workspace language + property country. |
| property_notes_md     | text        | internal, staff-visible        |
| welcome_defaults_json | jsonb       | wifi, doors, rules, contacts   |
| created_at/updated_at | tstz        |                                |
| deleted_at            | tstz null   | soft delete                    |

A property has one or more **units**, many **areas** (shared or
unit-scoped), and many **stays** (via units). It may have one or more
**iCal feeds** per unit (multiple platforms for the same listing).

### Billing client (agency mode)

A property may belong to a **billing client** via
`client_org_id` (§22 "`property.client_org_id`"). Semantics:

- **Null** (the default) preserves the pre-existing "the workspace
  is its own employer" shape: family-run households, single-owner
  vacation portfolios, etc. Vendor invoices at this property are
  paid by the workspace directly and no client-billing rollup
  applies.
- **Set** puts the property in agency mode: the workspace operates
  on behalf of a client, bills them for work done (via
  `client_rate` / `client_user_rate` resolution, §22), and rolls
  shift hours up into the per-client CSV export.

One property may have at most one `client_org_id` at a time.
Split-billing a single property across multiple clients is
explicitly out of scope (§22 "Out of scope").

### Multi-belonging (sharing across workspaces)

A property is **not** owned by a single workspace. The same physical
place can be linked to several workspaces simultaneously through
the `property_workspace` junction (§02 "Villa belongs to many
workspaces"), with one of three `membership_role` values:

- **`owner_workspace`** — exactly one. The party who can grant or
  revoke other workspaces' access. Typical client-owned scenario:
  the client's own workspace holds this row.
- **`managed_workspace`** — operational access granted by the owner
  (the agency that dispatches workers and creates work orders here).
- **`observer_workspace`** — read-only. Useful for a consulting party
  or a finance team that needs visibility without write rights.

Workers, shifts, work orders, and vendor invoices written under any
linked workspace carry that workspace's `workspace_id` forward, so
payroll, billing, and audit history stay separated even when several
teams share the same villa. Similarly, stay lifecycle rules fire
**per linked workspace** (§06 "Bundle generation logic") — each
workspace evaluates its own rules against the shared stay and
writes bundles tagged to itself. Duplicate coverage is expected
and not deduplicated: an owner workspace's "post-stay walkthrough"
and an agency workspace's "dispatch maid" both fire on the same
checkout without coordinating.

Switching the agency that manages a property is a
`property_workspace.revoke` (approval-gated, §22) followed by a
fresh `property_workspace_invite` (§22) to the new agency; the
client's `owner_workspace` link is what makes the revoke possible
without the outgoing agency's consent.

**PII boundary on shared properties.** By default a non-owner
linked workspace (`managed_workspace` / `observer_workspace`)
sees only the operational minimum — unit, dates, `guest_kind`,
tasks and evidence it authored itself — and not the guest name,
contact, or welcome-page personalisations. The owner workspace
may widen this per share via
`property_workspace.share_guest_identity`, which it sets when it
issues the invite (§22). Full boundary rules live in §15
"Cross-workspace visibility".

The web UI surfaces this on a per-property "Sharing & client" tab
(§14): list of memberships, the linked client organization,
pending invites, and the "Invite workspace" / "Revoke" controls
visible only to members of the owner workspace. Creating an
invite yields a shareable URL that the owner can copy and send
through any channel (WhatsApp, email, in person).

### `address_json` canonical shape

```json
{
  "line1": "12 Chemin des Oliviers",
  "line2": null,
  "city": "Antibes",
  "state_province": "Alpes-Maritimes",
  "postal_code": "06600",
  "country": "FR"
}
```

**Write rule:** on write, if `address_json.country` is provided the
server copies it to `property.country`; if only `property.country` is
set, `address_json.country` is back-filled.

### `kind` semantics

`kind` drives defaults for stay lifecycle rule seeding and area
seeding; managers can always override per-stay or per-task.

| kind       | auto-seed areas | default lifecycle rules                                     |
|------------|-----------------|-------------------------------------------------------------|
| `residence`| no              | none — no lifecycle rules auto-created                      |
| `vacation` | yes (per unit)  | one `after_checkout` rule linked to default turnover template |
| `str`      | yes (per unit)  | one `after_checkout` rule linked to default turnover template |
| `mixed`    | yes (per unit)  | same rules but with `guest_kind_filter = ['guest', 'staff', 'other']` |

Stays carry an optional `guest_kind` (`owner | guest | staff | other`;
default `guest`). Lifecycle rule evaluation is gated by the rule's
`guest_kind_filter` (§06).

### Welcome defaults

A JSON blob with fields used by the guest welcome page and also
available to staff:

```json
{
  "wifi": {"ssid": "VillaSud", "password": "…", "rotates": "per_stay"},
  "access": {"door_code": "…", "gate_code": "…", "lockbox": "…"},
  "house_rules_md": "No smoking. Quiet hours 22:00–08:00. …",
  "emergency_contacts": [
    {"label": "Manager", "name": "Alex", "phone_e164": "+33…"},
    {"label": "Plumber", "name": "…", "phone_e164": "…"}
  ],
  "local_tips_md": "Nearest pharmacy…",
  "trash_schedule_md": "Tuesdays and Fridays…"
}
```

If `wifi.rotates == "per_stay"`, the system expects a per-stay password
override; the welcome page falls back to the property default only if
no override is set.

## Unit

A bookable subdivision of a property. Every property has at least one
unit; single-unit properties auto-create a default unit with the unit
layer hidden in the UI. Stays, lifecycle bundles, and iCal feeds are
unit-scoped.

### Fields

| field                   | type      | notes                              |
|-------------------------|-----------|-------------------------------------|
| id                      | ULID PK   |                                    |
| workspace_id            | ULID FK   |                                    |
| property_id             | ULID FK   |                                    |
| name                    | text      | "Room 1", "Apt 3B", "Main house". For single-unit properties: property name (hidden in UI) |
| ordinal                 | int       | display order among siblings       |
| default_checkin_time    | time?     | nullable = inherit from property; e.g. `16:00` |
| default_checkout_time   | time?     | nullable = inherit from property; e.g. `10:00` |
| max_guests              | int?      | nullable = no limit                |
| welcome_overrides_json  | jsonb?    | per-unit overrides for wifi, access codes, etc. Merges with property `welcome_defaults_json` |
| settings_override_json  | jsonb?    | per-unit cascade layer (between property and work_engagement); see §02 "Settings cascade" |
| notes_md                | text?     |                                    |
| created_at / updated_at | tstz      |                                    |
| deleted_at              | tstz?     | soft delete                        |

### Invariants

- Every property has >= 1 unit (enforced by application, not DB
  constraint).
- When a property is created, a **default unit** is auto-created with
  `name = property.name` and `ordinal = 0`.
- Single-unit properties: UI hides the unit layer entirely (unit
  selector, unit management). The unit exists in the data model but
  is invisible.
- `UNIQUE(workspace_id, property_id, name)` — no two units in the
  same property share a name.

### Welcome overrides merge

The guest welcome page (§"Guest welcome link") merges unit-level
`welcome_overrides_json` over the property-level
`welcome_defaults_json`. Fields present in the unit override win;
absent fields fall through to the property default. For single-unit
properties this is a no-op (no overrides set).

## Area

An area is a subdivision of a property — kitchen, master bath, pool,
garage, garden, Room 3, etc. Areas exist so tasks, instructions, and
inventory can be scoped tightly.

### Fields

| field        | type      | notes                              |
|--------------|-----------|------------------------------------|
| id           | ULID PK   |                                    |
| property_id  | ULID FK   |                                    |
| unit_id      | ULID FK?  | null = shared/property-level area (pool, lobby, laundry). Non-null = unit-specific area (bedroom in Room 3) |
| name         | text      |                                    |
| kind         | enum      | `indoor_room | outdoor | service | ` ... |
| order_hint   | int       | for ordered UIs (walk order)       |
| parent_area  | ULID FK?  | optional sub-area (e.g. "Master suite" → "Master bath") |
| notes_md     | text      |                                    |

Areas nest **one level deep**; two levels are enough for every house
tested and deeper trees hurt the UI. Move operations enforce this.

### Auto-seeded areas

When a property is created with `kind = str`, we auto-seed a sensible
area set per unit (Entry, Kitchen, Living, Bedroom 1, Bathroom 1,
Outdoor, Trash & Laundry). Managers can rename or delete.

## Stay (reservation)

A reservation of a unit for a range of dates.

### Fields

| field              | type    | notes                                    |
|--------------------|---------|------------------------------------------|
| id                 | ULID PK |                                          |
| property_id        | ULID FK | denormalized from `unit.property_id` for query efficiency; enforced by trigger/application constraint |
| unit_id            | ULID FK | the unit this stay occupies              |
| source             | enum    | §02 `stay_source`                        |
| external_id        | text    | provider reservation id (nullable)       |
| check_in_at        | tstz    | local rendered in property tz            |
| check_out_at       | tstz    |                                          |
| guest_name         | text    | best-effort (iCal often redacts)         |
| guest_count        | int     |                                          |
| guest_kind         | enum    | `owner | guest | staff | other` (default `guest`); gates lifecycle rules via `guest_kind_filter` |
| nightly_rate_cents | int     | optional, only for owner stays           |
| status             | enum    | `tentative | confirmed | in_house | checked_out | cancelled` |
| notes_md           | text    |                                          |
| wifi_password_override | text |                                          |
| guest_link_id      | ULID FK | 0/1 `guest_link`                         |

Status transitions are the natural order. `in_house` is triggered at
`check_in_at` by the worker; `checked_out` at `check_out_at` or on
manager action.

### Stay task bundles

When a stay has `status IN (tentative, confirmed)` and its check-in
or check-out falls within the scheduling horizon, the worker
evaluates the property's (and unit's) **stay lifecycle rules** (§06)
and creates **stay task bundles** — one `STAY_TASK_BUNDLE` row per
matching rule, each containing 1..N `task` rows generated from the
rule's template. Tasks in a bundle share `stay_task_bundle_id`.

Three trigger types drive bundle generation:

- **`after_checkout`** — tasks to perform after the guest leaves
  (turnover cleaning, linen change, inventory restock). Same logic as
  the former turnover bundle.
- **`before_checkin`** — tasks to perform before the guest arrives
  (welcome prep, fresh flowers, pre-arrival inspection). Subject to
  pull-back scheduling (§06) when the ideal date falls on an
  unavailable day.
- **`during_stay`** — recurring tasks within the stay bounds (weekly
  linen change for long stays). Driven by an RRULE on the rule.

Bundles are created once per (stay, rule) pair, live across stay
edits, and show in the UI as coherent groups. **Edit semantics:**

- For `after_checkout` bundles: if
  `|new.check_out_at - old.check_out_at| < 4h` **and** the stay is
  not yet in `checked_out` state: patch `scheduled_for_local`,
  `scheduled_for_utc`, `due_by_utc`, and `assigned_user_id` on
  the existing bundle's tasks in place (state-gated to
  `scheduled | pending`).
- For `before_checkin` bundles: same logic keyed on
  `check_in_at` shift.
- Otherwise: cancel the existing bundle's `scheduled | pending` tasks
  with `cancellation_reason = 'stay rescheduled'` and regenerate from
  the rule's template.

### Overlap detection

Two stays for the same **unit** whose `[check_in_at, check_out_at)`
overlap raise a **conflict**. Stays at different units in the same
property do not conflict. Conflicts are visible in the UI, are
delivered in the daily digest (§10), and do **not** prevent the second
insert. Rationale: iCal feeds frequently double-book for a few minutes
while a cancellation propagates; we flag and let a human decide.

## iCal feed

Each feed is a URL the worker polls on schedule. Stays are upserted by
`(unit_id, source, external_id)` when `unit_id` is set, or by
`(property_id, source, external_id)` when null (existing behavior for
single-unit properties where the manager has not mapped feeds to
units).

### Fields

| field         | type      |
|---------------|-----------|
| id            | ULID PK   |
| property_id   | ULID FK   |
| unit_id       | ULID FK?  | when set, stays from this feed are created for that unit. When null, stays go to the default unit (or manager maps manually) |
| source        | enum      |
| url           | text (secret — envelope-encrypted, §15) |
| poll_cadence  | text      | default `*/15 * * * *` |
| enabled       | bool      |
| last_polled_at| tstz      |
| last_etag     | text      |
| last_error    | text      |

### Polling behavior

- Use `If-None-Match` / `If-Modified-Since` to avoid re-downloads.
- Parse with `icalendar`. VEVENTs whose `SUMMARY` signals
  "Not available" or "Blocked" (Airbnb conventions) are upserted as
  **`property_closure`** rows (§06) with `reason = ical_unavailable`
  and `source_ical_feed_id` set — not as stays. Managers see them on
  the calendar with a distinct swatch; deleting them manually is
  allowed (the next poll will not recreate them unless the underlying
  VEVENT reappears upstream).
- Diff ordinary VEVENTs against existing stays: create, update, or
  cancel.
- Surface parse errors as `issue` against the feed.
- Rate-limit per host; respect provider 429s.

### Supported providers

- **Airbnb** export feeds (`*.ics` per listing).
- **VRBO / Expedia Partner** export feeds.
- **Booking.com** calendar sync URL.
- **Google Calendar** public ICS.
- **Generic ICS** (fallback, same parser).

Provider-specific quirks go in `adapters/ical/providers/*.py` with
unit tests for each.

## Guest welcome link

A tokenized URL shared with each guest. No login. Read-only.

### Fields

| field           | type    |
|-----------------|---------|
| id              | ULID PK |
| stay_id         | ULID FK |
| token           | text    | signed, opaque                           |
| expires_at      | tstz    | defaults to `check_out_at + 1d`          |
| revoked_at      | tstz?   |                                          |
| access_log_json | jsonb   | last 10 accesses (IP prefix + UA family) |

### What the page shows

- Property name (and unit name for multi-unit properties) + cover
  photo (optional upload per property).
- Dates of stay, local time.
- Wifi SSID + password (stay override wins over unit override wins
  over property default).
- Access info (door codes, lockbox, parking — unit
  `welcome_overrides_json` merged over property `welcome_defaults_json`).
- House rules.
- Trash/recycling schedule.
- Local tips (markdown from property or manager-overridden per stay).
- **Check-out checklist** — a subset of the stay task bundle's
  checklist flagged `guest_visible = true`, rendered as a simple
  "Before you leave:" list.
- Emergency contacts.
- A "Report an issue" button that opens a mailto link to the manager.
- **Equipment** — when `assets.show_guest_assets` is `true` for the
  property (§02 settings cascade, §21), lists assets where
  `guest_visible = true`. Each entry shows the asset name,
  `guest_instructions_md` (rendered markdown), and cover photo if set.
  Omitted entirely when no visible assets exist or the setting is
  `false`.

### Privacy

- The link is unpredictable (see §03 for token format).
- Link can be revoked by the manager at any time → page returns 410
  Gone with a friendly "This welcome link has been turned off. If you
  need the information again, please ask your host." message. The
  check-out checklist is hidden; no stay data is rendered.
- The same 410 page is served on natural expiry (`expires_at` reached);
  only the wording differs ("This link has expired"). Both cases log
  the access with no stay payload.
- Access log captures hashed-IP-prefix + UA-family only, never full IP.
- No cookies on the guest page. No JS beyond the small manifest that
  renders the check-out checklist.

## Airbnb-style edge cases

- **Same-day turnovers (same unit).** When `prev.check_out_at` equals
  `next.check_in_at` on the same unit, `after_checkout` bundles are
  time-boxed to the gap between check-out and check-in;
  `before_checkin` rules with `suppress_if_turnaround_under_hours`
  matching the gap are suppressed (the turnover covers pre-arrival).
- **Long stays / owner stays.** `source = manual` can carry
  `status = in_house` for months; inventory and schedules still run
  but lifecycle rules with `guest_kind_filter` excluding `owner`
  do not fire.
- **Back-to-back with no cleaning window.** System surfaces a warning
  in the daily digest and creates a lower-severity issue. Managers
  can override.
- **Cancellations after check-in.** Rare; handled by `status =
  cancelled` + preservation of any shifts already logged.
- **Multiple units, same-day turnovers.** Each unit generates its own
  bundles independently. The assignment algorithm's "fewest tasks in
  7-day window" tiebreaker naturally distributes work across staff.

## CLI (examples, see §13)

```
crewday properties add "Villa Sud" --tz Europe/Paris --kind str
crewday units add <prop> "Room 1" --checkin-time 16:00 --checkout-time 10:00
crewday units list <prop>
crewday areas add <prop> "Pool" --kind outdoor
crewday areas add <prop> "Bedroom 1" --kind indoor_room --unit <unit>
crewday ical add --property <prop> --unit <unit> --source airbnb --url https://...
crewday stays list --property <prop> --upcoming 14d
crewday stays welcome-link <stay-id>   # emits the guest URL
```
