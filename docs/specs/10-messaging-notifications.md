# 10 — Messaging, notifications, webhooks

## Channels

v1 ships one human messaging channel plus the in-app agent surfaces:

1. **Email** — every out-message originated by a **human** (owner/
   manager-authored mention, notification, digest) goes here, and
   here only.

Cross-user messaging stays on email (§10) and task threads (§06).
The embedded agent conversations in the web sidebar and worker PWA
Chat tab are the only shipped chat transports in v1. Off-app chat
adapters (WhatsApp, SMS, Telegram, push) are intentionally **not
enabled in v1**; §23 is retained as a deferred design reference so
those transports can be added later without re-architecting the
runtime.

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

| event                          | to                                   | required?     |
|--------------------------------|--------------------------------------|---------------|
| magic link (enrollment / recovery) | recipient                        | yes           |
| daily owner/manager digest     | each user with owner or manager grant | opt-out      |
| daily worker digest            | each user with worker grant          | opt-out       |
| task overdue alert             | assigned user + owner/manager        | opt-out       |
| task comment mention           | mentioned user                       | opt-out       |
| issue reported                 | owners and managers                  | yes           |
| expense submitted              | owners and managers                  | yes           |
| expense decision               | submitting user                      | yes           |
| payslip issued                 | work-engagement user                 | yes           |
| iCal feed error                | owners and managers                  | yes           |
| anomaly detected (§11)         | owners and managers                  | opt-out       |
| availability override pending  | owners and managers                  | yes           |
| pre-arrival task unassigned    | owners and managers                  | yes           |
| holiday schedule impact        | affected users                       | opt-out       |
| agent approval pending         | owners and managers                  | yes           |

Opt-outs are per-person, per-category, via a signed unsubscribe link
in the footer of each email. Required emails (security-relevant, or
legally equivalent) cannot be unsubscribed but throttle by priority.

### `email_opt_out`

| field         | type    | notes                                          |
|---------------|---------|------------------------------------------------|
| id            | ULID PK |                                                |
| workspace_id  | ULID FK |                                                |
| user_id       | ULID FK |                                                |
| category      | text    | matches `email_delivery.template_key` family   |
| opted_out_at  | tstz    |                                                |
| source        | enum    | `unsubscribe_link | profile | admin`           |

Before sending, the worker checks for an `email_opt_out` row matching
`(workspace_id, user_id, category)`. Required categories (magic link,
payslip issued, expense decision, issue reported, agent approval
pending) are never suppressed even if a row exists — the row is kept
for audit but ignored for those templates.

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

- **Owner/manager digest** — today's upcoming tasks, stays arriving/
  leaving, overdue tasks, open issues, pending approvals (incl.
  availability override requests), low-stock items, warranties/
  certificates expiring soon (within `assets.warranty_alert_days`,
  §21), iCal errors, anomalies, expenses awaiting review, **unassigned
  pre-arrival tasks** (pull-back failed), **upcoming public holidays
  with scheduling impact**.
- **Worker digest** — "Today you have X tasks", grouped by property,
  with a quick link to the PWA.

## In-app messaging

The in-app messaging surface is the **task-scoped agent thread**
(§06 "Task notes are the agent inbox"). A `task_comment` row is no
longer a free list of user comments — it is an **event in the log
of a workspace-agent-mediated conversation** scoped to that task.

Message kinds in the log: `user | agent | system`. The workspace
agent (§11) is a **full participant**: it reads every message as it
is posted, can summarise the thread on demand, answer questions
grounded in instructions (§07), and speak in the thread on delegation
("@agent remind Maria the linen press is below her required
temperature"). Owners and managers read and reply through the same
thread on the desktop chat surface (§14); workers read and reply
through the worker chat page. Human `@mentions` resolve to workspace
members and still trigger email fallback for offline recipients.

There are still no DMs and no group chats outside a task thread.
If a manager wants a free-form conversation, they use the right-
sidebar workspace agent (§14), whose actions are audited like any
other agent write.

### Off-app agent reach-out

Off-app agent reach-out is intentionally **not enabled in shipped
v1**. The future design still assumes:

- User ↔ **agent** conversation, not human ↔ human DM.
- Opt-in is **implicit in the binding** — presence of an active
  `chat_channel_binding` means agent reach-out is on; unlinking it
  is the opt-out. No separate `preferred_offapp_channel` toggle.
- Notification timing is the user's phone's job (OS-level
  do-not-disturb, WhatsApp's own mute). The product does not carry
  its own quiet-hours window. Per-binding `PAUSE <duration>` still
  works for ad-hoc silence (§23).
- Daily-cap controls at the workspace level still apply.
- A shared `chat_message` / `chat_thread` substrate rather than a
  separate delivery model.

See §23 for the deferred reference design and §15 for the privacy
rules that should apply when these adapters are eventually enabled.

### Auto-translation

When a user writes a message in a language other than the workspace's
`default_language` (§02), the agent:

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

3. Owners and managers see the workspace-default-language copy by
   default, with a **toggle** on the message to reveal the original.
   Workers see their own original plus the auto-translated copy if an
   owner or manager replies in a different language.

Agent-originated outbound messages are generated directly in the
user's `languages[0]` (§05) when known, falling back to the workspace
default. No second translation is stored for those — the message is
written once in the target language and the provenance is the
`llm_call` row.

See §18 for the broader translation policy.

## Issue reports

Any user taps **"Report an issue"** from a property/area context or
from a task:

```
issue
├── id
├── workspace_id
├── reported_by_user_id
├── property_id / area_id      # either
├── task_id?                   # if raised from a task
├── title
├── description_md
├── severity                   # low | normal | high | urgent
├── category                   # damage | broken | supplies | safety | other
├── state                      # open | in_progress | resolved | wont_fix
├── attachment_file_ids        # ULID[]; each id references `file` (§02)
├── converted_to_task_id       # when an owner or manager escalates
├── resolution_note
├── resolved_at
├── resolved_by
├── created_at / updated_at
└── deleted_at
```

Owner/manager actions: convert to task (one click → creates a handyman
task linked back to the issue), change state, add notes. Reporters see
state changes on their issue and can comment. Email to reporter on
resolution.

## Webhooks (outbound)

An agent or external system subscribes to events.

### Subscription

`POST /api/v1/webhooks`:

```json
{
  "name": "hermes-prod",
  "url": "https://hermes.example.com/crewday",
  "secret": "optional; system generates if omitted",
  "events": ["task.completed", "stay.upcoming"],
  "active": true
}
```

### Event catalog (v1)

```
user.*               created, updated, archived, reinstated
role_grant.*         granted, revoked, updated
work_engagement.*    created, updated, archived, reinstated,
                     engagement_kind_changed
task.*               created, assigned, updated, started, completed,
                     complete_superseded, skipped, cancelled, overdue,
                     unassigned_pre_arrival
task_comment.*       created
stay.*               created, updated, upcoming, in_house, checked_out,
                     cancelled, conflict
stay_lifecycle_rule.* created, updated, deleted
stay_task_bundle.*   created, completed, cancelled
instruction.*        created, published, archived
inventory.*          low_stock, movement, stock_drift
chat_channel_binding.* created, verified, revoked, link_expired
chat_message.*       received, sent, delivered, failed
chat_thread.*        opened, archived
shift.*              opened, closed, adjusted, disputed
expense.*            submitted, approved, rejected, reimbursed
payroll.*            period_opened, period_locked, period_paid,
                     payslip_issued, payslip_paid,
                     payslip_destination_snapshotted,
                     payout_manifest_accessed
payout_destination.* created, updated, archived, verified
work_engagement_default_destination.* set, cleared
issue.*              reported, updated, resolved
approval.*           pending, decided
ical.*               polled, error
asset.*              created, updated, condition_changed,
                     status_changed, deleted, restored
asset_action.*       created, updated, performed,
                     schedule_linked, deleted
asset_document.*     created, updated, deleted, expiring
leave.*              requested, approved, rejected
availability_override.* created, approved, rejected
public_holiday.*     created, updated, deleted
property_closure.*   created, updated, deleted
organization.*       created, updated, archived
client_rate.*        created, updated, archived
work_order.*         created, state_changed, accept_quote,
                     cancelled, deleted
quote.*              submitted, accepted, rejected, superseded
vendor_invoice.*     submitted, approved, rejected, paid, voided
shift_billing.*      resolved
agent_preference.*   updated, cleared  (§11)
```

The `manager.*` and `employee.*` event families from earlier drafts
are replaced by `user.*`, `role_grant.*`, and `work_engagement.*`.
Subscribers that previously watched `manager.*` or `employee.*`
should update to `user.*`; `role_grant.*` covers permission lifecycle
and `work_engagement.*` covers employment/pay-pipeline lifecycle.

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
- `X-Crewday-Signature: t=<unix>,v1=<hex HMAC-SHA256>`
  over `t.raw_body`; secret is the subscription's secret.
- `X-Crewday-Event`, `X-Crewday-Delivery`.

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
crewday webhooks add --name hermes --url https://… \
                       --events task.completed,stay.upcoming
crewday notifications test --to owner@example.com --template daily_digest
crewday issues list --property prop_… --state open
crewday comments post <task-id> "ping @maria re: linens"
```
