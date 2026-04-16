# 10 — Messaging, notifications, webhooks

## Channels

v1 ships three outbound messaging channels to humans:

1. **Email** — every out-message originated by a **human** (manager-
   authored mention, notification, digest) goes here, and here only.
2. **WhatsApp** — **agent-originated** out-messages only. The
   workspace agent (§11) may reach an employee on WhatsApp for
   self-solvable, low-stakes checks ("did you finish the kitchen?",
   "please confirm the chlorine level", "your leave is approved").
3. **SMS** — fallback for WhatsApp when the employee's number is
   unreachable on WhatsApp or their capability flag disables it.
   Also agent-only.

Humans cannot initiate WhatsApp or SMS out-messages — the agent is
the only channel operator that may use them. This keeps the cost,
compliance, and privacy surface (§15) tied to a single, well-
understood sender. Additional channels (push, Slack, Matrix) remain
deferred; §18 documents the seam.

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

**Locale-aware template resolution.** The system resolves templates
with locale fallback: it looks for `{key}_{locale}.html`, then
`{key}_{language}.html`, then `{key}.html`. v1 ships only English
defaults; the resolution logic is in place from day one. All templates
receive `locale` in their Jinja context. Formatting helpers
(`fmt_date`, `fmt_money`, `fmt_number`) respect this parameter.

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
| workspace_id  | ULID FK |                                                |
| person_kind   | enum    | `manager | employee`                           |
| person_id     | ULID    |                                                |
| category      | text    | matches `email_delivery.template_key` family   |
| opted_out_at  | tstz    |                                                |
| source        | enum    | `unsubscribe_link | profile | admin`           |

Before sending, the worker checks for an `email_opt_out` row matching
`(workspace_id, person_kind, person_id, category)`. Required categories
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

The in-app messaging surface is the **task-scoped agent thread**
(§06 "Task notes are the agent inbox"). A `task_comment` row is no
longer a free list of user comments — it is an **event in the log
of a workspace-agent-mediated conversation** scoped to that task.

Message kinds in the log: `employee | manager | agent | system`.
The workspace agent (§11) is a **full participant**: it reads every
message as it is posted, can summarise the thread on demand, answer
questions grounded in instructions (§07), and speak in the thread
on delegation ("@agent remind Maria the linen press is below her
required temperature"). Managers read and reply through the same
thread on the desktop chat surface (§14); employees read and reply
through the employee chat page. Human `@mentions` resolve to
workspace members and still trigger email fallback for offline
recipients.

There are still no DMs and no group chats outside a task thread.
If a manager wants a free-form conversation, they use the right-
sidebar workspace agent (§14), whose actions are audited like any
other agent write.

### Off-app agent reach-out

The workspace agent may initiate outbound conversation with an
employee over **WhatsApp** (and **SMS** fallback) for low-stakes,
self-solvable checks:

- "did you finish the kitchen?" with a one-tap confirm link.
- "please confirm the chlorine level at Villa Sud is between 1 and
  3 ppm before you leave."
- "your leave for April 22 has been approved — reply STOP to these
  messages anytime."

Reach-out is **agent-only**. Humans cannot compose a WhatsApp or
SMS message from inside miployees; they interact with the employee
either via email (§10 above) or via the in-app task thread, which
the agent will mirror to WhatsApp if it decides reach-out is useful.

Rules:

- Each employee has `preferred_offapp_channel` (`whatsapp | sms |
  none`) and a `phone_e164_whatsapp` pair of fields. `none` opts
  out.
- Reach-out respects a per-employee quiet-hours window (default
  21:00-08:00 local) and a per-workspace daily cap on agent-
  originated messages per employee (default 5/day).
- Inbound replies route back into the task thread they originated
  from (or into a fresh "ad-hoc" thread if none); the agent is
  responsible for summarising the reply for the manager.
- Delivery is tracked in an `offapp_delivery` row mirroring
  `email_delivery` above (states `queued | sent | delivered |
  read | failed`).
- WhatsApp provider is configurable (Meta Cloud API by default);
  SMS via any RFC-compliant SMS provider. Credentials live in
  `secret_envelope`.

See §15 for the privacy rules around WhatsApp/SMS numbers — they
are PII and are redacted from upstream LLM prompts by default.

### Auto-translation

When an employee writes a message in a language other than the
workspace's `default_language` (§02), the agent:

1. Detects the language of the inbound message (`llm_call` with
   capability `chat.detect_language`).
2. Stores **both** the detected original message **and** a machine-
   translated copy in the workspace default language on the
   `task_comment` row:

   ```
   task_comment.body_md                # translated copy (workspace default lang)
   task_comment.body_md_original       # as written
   task_comment.language_original      # BCP-47, detected
   task_comment.translation_llm_call_id
   ```

3. Managers see the workspace-default-language copy by default, with
   a **toggle** on the message to reveal the original. Employees see
   their own original plus the auto-translated copy if the manager
   replies in a different language.

Agent-originated outbound messages are generated directly in the
employee's `languages[0]` (§05) when known, falling back to the
workspace default. No second translation is stored for those —
the message is written once in the target language and the
provenance is the `llm_call` row.

See §18 for the broader translation policy.

## Issue reports

Employee taps **"Report an issue"** from a property/area context or
from a task:

```
issue_report
├── id
├── workspace_id
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
                     payslip_issued, payslip_paid,
                     payslip_destination_snapshotted,
                     payout_manifest_accessed
payout_destination.* created, updated, archived, verified
employee_default_destination.* set, cleared
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
