# 04 — Properties, areas, stays, guests

## Property

A property is a physical place the household operates in. The primary
residence, a vacation home, an STR unit, or a yacht all count.

### Fields

| field                 | type        | notes                          |
|-----------------------|-------------|--------------------------------|
| id                    | ULID PK     |                                |
| household_id          | ULID FK     |                                |
| name                  | text        | "Villa Sud", "Apt 3B"          |
| kind                  | enum        | `residence | vacation | str | mixed` (behavior below) |
| address_json          | jsonb/text  | structured address             |
| timezone              | text        | IANA (`Europe/Paris`)          |
| default_currency      | text        | ISO 4217, inherits household   |
| property_notes_md     | text        | internal, staff-visible        |
| welcome_defaults_json | jsonb       | wifi, doors, rules, contacts   |
| created_at/updated_at | tstz        |                                |
| deleted_at            | tstz null   | soft delete                    |

A property has many **areas** and many **stays**. It may have one or
more **iCal feeds** (multiple platforms for the same unit).

### `kind` semantics

`kind` drives defaults for turnover generation and area seeding;
managers can always override per-stay or per-task.

| kind       | auto-seed areas | turnover generation                                         |
|------------|-----------------|-------------------------------------------------------------|
| `residence`| no              | none — a residence does not turn over on guest checkout     |
| `vacation` | yes             | every stay generates a turnover bundle                      |
| `str`      | yes             | every stay generates a turnover bundle                      |
| `mixed`    | yes             | turnover only for stays where `guest_kind != owner`         |

Stays carry an optional `guest_kind` (`owner | guest | staff | other`;
default `guest`). The turnover decision is made by §06 at stay-create
time.

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

## Area

An area is a subdivision of a property — kitchen, master bath, pool,
garage, garden, Room 3, etc. Areas exist so tasks, instructions, and
inventory can be scoped tightly.

### Fields

| field        | type      | notes                              |
|--------------|-----------|------------------------------------|
| id           | ULID PK   |                                    |
| property_id  | ULID FK   |                                    |
| name         | text      |                                    |
| kind         | enum      | `indoor_room | outdoor | service | ` ... |
| order_hint   | int       | for ordered UIs (walk order)       |
| parent_area  | ULID FK?  | optional sub-area (e.g. "Master suite" → "Master bath") |
| notes_md     | text      |                                    |

Areas nest **one level deep**; two levels are enough for every house
tested and deeper trees hurt the UI. Move operations enforce this.

### Auto-seeded areas

When a property is created with `kind = str`, we auto-seed a sensible
area set (Entry, Kitchen, Living, Bedroom 1, Bathroom 1, Outdoor,
Trash & Laundry). Managers can rename or delete.

## Stay (reservation)

A reservation of a property by a guest for a range of dates.

### Fields

| field              | type    | notes                                    |
|--------------------|---------|------------------------------------------|
| id                 | ULID PK |                                          |
| property_id        | ULID FK |                                          |
| source             | enum    | §02 `stay_source`                        |
| external_id        | text    | provider reservation id (nullable)       |
| check_in_at        | tstz    | local rendered in property tz            |
| check_out_at       | tstz    |                                          |
| guest_name         | text    | best-effort (iCal often redacts)         |
| guest_count        | int     |                                          |
| guest_kind         | enum    | `owner | guest | staff | other` (default `guest`); gates turnover for `property.kind = mixed` |
| nightly_rate_cents | int     | optional, only for owner stays           |
| status             | enum    | `tentative | confirmed | in_house | checked_out | cancelled` |
| notes_md           | text    |                                          |
| wifi_password_override | text |                                          |
| guest_link_id      | ULID FK | 0/1 `guest_link`                         |

Status transitions are the natural order. `in_house` is triggered at
`check_in_at` by the worker; `checked_out` at `check_out_at` or on
manager action.

### Turnover bundles

When a stay's `check_out_at` lands in the future **and** the property's
`kind` permits turnover (see table above), the worker creates a
**turnover bundle** — one `TURNOVER_BUNDLE` row and 1..N `task` rows
generated from the household's turnover template for that property
(see §06). Tasks in a bundle share `turnover_bundle_id`.

Turnover is created once per stay, lives across stay edits, and shows
in the UI as a coherent group. **Edit semantics:**

- If `|new.check_out_at - old.check_out_at| < 4h` **and** the stay is
  not yet in `checked_out` state: patch `scheduled_for_local`,
  `scheduled_for_utc`, `due_by_utc`, and `assigned_employee_id` on the
  existing bundle's tasks in place (state-gated to
  `scheduled | pending`).
- Otherwise: cancel the existing bundle's `scheduled | pending` tasks
  with `cancellation_reason = 'stay rescheduled'` and generate a new
  bundle from the template.

### Overlap detection

Two stays for the same property whose `[check_in_at, check_out_at)`
overlap raise a **conflict**. Conflicts are visible in the UI, are
delivered in the daily digest (§10), and do **not** prevent the second
insert. Rationale: iCal feeds frequently double-book for a few minutes
while a cancellation propagates; we flag and let a human decide.

## iCal feed

Each feed is a URL the worker polls on schedule. Stays are upserted by
`(property_id, source, external_id)`.

### Fields

| field         | type      |
|---------------|-----------|
| id            | ULID PK   |
| property_id   | ULID FK   |
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
- Surface parse errors as `issue_report` against the feed.
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

- Property name + cover photo (optional upload per property).
- Dates of stay, local time.
- Wifi SSID + password (stay override wins over property default).
- Access info (door codes, lockbox, parking).
- House rules.
- Trash/recycling schedule.
- Local tips (markdown from property or manager-overridden per stay).
- **Check-out checklist** — a subset of the turnover task's checklist
  flagged `guest_visible = true`, rendered as a simple
  "Before you leave:" list.
- Emergency contacts.
- A "Report an issue" button that opens a mailto link to the manager.

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

- **Same-day turnovers.** When `prev.check_out_at` equals
  `next.check_in_at`, turnover is time-boxed to the gap between
  providers' defaults (usually 11:00 → 16:00); template default
  overrides apply.
- **Long stays / owner stays.** `source = manual` can carry
  `status = in_house` for months; inventory and schedules still run
  but turnover templates do not fire.
- **Back-to-back with no cleaning window.** System surfaces a warning
  in the daily digest and creates a lower-severity issue. Managers
  can override.
- **Cancellations after check-in.** Rare; handled by `status =
  cancelled` + preservation of any shifts already logged.

## CLI (examples, see §13)

```
miployees properties add "Villa Sud" --tz Europe/Paris --kind str
miployees areas add <prop> "Pool" --kind outdoor
miployees ical add --property <prop> --source airbnb --url https://...
miployees stays list --property <prop> --upcoming 14d
miployees stays welcome-link <stay-id>   # emits the guest URL
```
