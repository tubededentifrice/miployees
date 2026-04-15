# 10 — Messaging, notifications, webhooks

## Channels

v1 ships exactly **one** outbound messaging channel to humans:
**email**. Everything else (SMS, WhatsApp, push, Slack) is left as a
deliberate non-goal per the user's selection; §18 documents the seam.

## Email

### Provider

SMTP (RFC 5321). Config:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `SMTP_SECURE` (`starttls` | `tls` | `none`)
- `MAIL_FROM`, `MAIL_REPLY_TO`

Provider-agnostic so the user can wire up Postmark, SES, their own
Postfix, or Resend via SMTP bridge.

### Template system

Jinja2 templates under `app/templates/email/`. MJML compiled at build
time into plain HTML. Every email is **both** HTML and plaintext. No
external CSS. Preheader text as a hidden first div.

### Emails the system sends

| event                          | to                      | required?     |
|--------------------------------|-------------------------|---------------|
| magic link (enrollment / recovery) | recipient           | yes           |
| daily manager digest           | each manager            | opt-out       |
| daily employee digest          | each employee           | opt-out       |
| task overdue alert             | assignee + manager      | opt-out       |
| task comment mention           | mentioned person        | opt-out       |
| issue reported                 | managers                | yes           |
| expense submitted              | managers                | yes           |
| expense decision               | submitting employee     | yes           |
| payslip issued                 | employee                | yes           |
| iCal feed error                | managers                | yes           |
| anomaly detected (§11)         | managers                | opt-out       |
| agent approval pending         | managers                | yes           |

Opt-outs are per-person, per-category, via a signed unsubscribe link
in the footer of each email. Required emails (security-relevant, or
legally equivalent) cannot be unsubscribed but throttle by priority.

### `email_opt_out`

| field         | type    | notes                                          |
|---------------|---------|------------------------------------------------|
| id            | ULID PK |                                                |
| household_id  | ULID FK |                                                |
| person_kind   | enum    | `manager | employee`                           |
| person_id     | ULID    |                                                |
| category      | text    | matches `email_delivery.template_key` family   |
| opted_out_at  | tstz    |                                                |
| source        | enum    | `unsubscribe_link | profile | admin`           |

Before sending, the worker checks for an `email_opt_out` row matching
`(household_id, person_kind, person_id, category)`. Required categories
(magic link, payslip issued, expense decision, issue reported, agent
approval pending) are never suppressed even if a row exists — the row
is kept for audit but ignored for those templates.

### Delivery tracking

```
email_delivery
├── id
├── to_person_id
├── to_email_at_send           # snapshot
├── template_key
├── context_snapshot_json
├── sent_at
├── provider_message_id
├── delivery_state             # queued | sent | delivered | bounced | failed
├── first_error
├── retry_count
└── inbound_linkage            # reply-tracking, if any
```

### Daily digests

Sent at 07:00 local time per recipient (their timezone), by the
worker. Retries if SMTP fails; skipped if no noteworthy content.

- **Manager digest** — today's upcoming tasks, stays arriving/leaving,
  overdue tasks, open issues, pending approvals, low-stock items,
  iCal errors, anomalies, expenses awaiting review.
- **Employee digest** — "Today you have X tasks", grouped by property,
  with a quick link to the PWA.

## In-app messaging

Only one in-app messaging surface: **task comments** (§06).

Threaded per task, markdown, `@mentions`, email notification for
mentions. No DMs, no group chats, no presence. If managers want that,
they run WhatsApp.

## Issue reports

Employee taps **"Report an issue"** from a property/area context or
from a task:

```
issue_report
├── id
├── household_id
├── reported_by_employee_id
├── property_id / area_id      # either
├── task_id?                   # if raised from a task
├── title
├── description_md
├── severity                   # low | normal | high | urgent
├── state                      # open | in_progress | resolved | wont_fix
├── attachment_file_ids        # ULID[]; each id references `file` (§02)
├── converted_to_task_id       # when a manager escalates
├── resolution_note
├── resolved_at
├── resolved_by
├── created_at / updated_at
└── deleted_at
```

Manager actions: convert to task (one click → creates a handyman task
linked back to the issue), change state, add notes. Employees see
state changes on their issue and can comment. Email to reporter on
resolution.

## Webhooks (outbound)

An agent or external system subscribes to events.

### Subscription

`POST /api/v1/webhooks`:

```json
{
  "name": "hermes-prod",
  "url": "https://hermes.example.com/miployees",
  "secret": "optional; system generates if omitted",
  "events": ["task.completed", "stay.upcoming"],
  "active": true
}
```

### Event catalog (v1)

```
manager.*            created, updated, archived, reinstated
employee.*           created, updated, archived, reinstated
task.*               created, assigned, started, completed,
                     complete_superseded, skipped, cancelled, overdue
task_comment.*       created
stay.*               created, updated, upcoming, in_house, checked_out,
                     cancelled, conflict
instruction.*        created, published, archived
inventory.*          low_stock, movement, stock_drift
shift.*              opened, closed, adjusted, disputed
expense.*            submitted, approved, rejected, reimbursed
payroll.*            period_opened, period_locked, period_paid,
                     payslip_issued, payslip_paid
issue.*              reported, updated, resolved
approval.*           pending, decided
ical.*               polled, error
leave.*              requested, approved, rejected
property_closure.*   created, updated, deleted
```

There is no `person.*` family — earlier drafts used it as a
supertype, but managers and employees emit their own events. Webhook
subscribers that want both can subscribe with
`events: ["manager.*", "employee.*"]`.

### Envelope

```json
{
  "event": "task.completed",
  "delivered_at": "…",
  "delivery_id": "whd_01J…",
  "data": { … event-specific payload … }
}
```

Headers:
- `X-Miployees-Signature: t=<unix>,v1=<hex HMAC-SHA256>`
  over `t.raw_body`; secret is the subscription's secret.
- `X-Miployees-Event`, `X-Miployees-Delivery`.

### Retries

- On 2xx → delivered.
- On non-2xx or timeout (10s): exponential backoff (1m, 5m, 30m, 2h,
  12h), with a per-delivery cap of 48h. After 48h the delivery is
  marked `failed` and dropped.
- A subscription whose last 24h of deliveries are all non-2xx is
  marked `unhealthy` and **paused** (no new deliveries enqueued). The
  manager is notified. A manager or a token with
  `messaging:write` can call `POST /webhooks/{id}/enable` to resume;
  enabling re-opens the queue but does not replay the dropped
  deliveries (use `/replay` for that).

### Delivery log retention

`webhook_delivery` rows are retained for 90 days by default
(configurable; see §02 operational-log retention).

### Replay / backfill

`POST /api/v1/webhooks/{id}/replay {since, until, events[]}` replays
the matching events. Idempotency is the receiver's responsibility,
but every delivery carries a stable `delivery_id`.

## CLI (examples)

```
miployees webhooks add --name hermes --url https://… \
                       --events task.completed,stay.upcoming
miployees notifications test --to owner@example.com --template daily_digest
miployees issues list --property prop_… --state open
miployees comments post <task-id> "ping @maria re: linens"
```
