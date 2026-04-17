# 11 — LLM integration and agents

Per the user's direction: **Google Gemma 4 31B IT via OpenRouter**
(`google/gemma-4-31b-it`) is the default model, with a per-capability
assignment table so other models can substitute for specific jobs.
All in-app agentic features (natural-language task intake, daily
digest, anomaly detection, receipt OCR, staff chat assistant, agent
audit trail, action approval, embedded owner/manager and worker chat
agents) share the same plumbing.

## The agent-first invariant

miployees is built around a hard rule: **every human UI verb exists
as a CLI or REST command first, and the UI is a shell around those
commands.** There is no owner/manager-only button, no worker-only
button, that cannot also be driven by the CLI (§13) or by an agent
holding a delegated token from the calling user. Concretely:

- Every form in §14 posts to an endpoint documented in §12.
- Every endpoint in §12 has a matching CLI command in §13. This
  mapping is enforced structurally: each API route carries an `x-cli`
  OpenAPI extension that defines its CLI command, and a CI parity gate
  (§17) fails the build if any endpoint lacks a CLI mapping. See §13
  "CLI generation from OpenAPI" for the mechanism.
- Every action in §13 is reachable as a tool call from one of the
  two embedded chat agents described below, using a delegated token
  that inherits the calling user's full permissions (§03).
- Dangerous actions are not hidden from agents; they are **gated**
  by §11's approval pipeline, by the "interactive-session-only
  endpoints" list, or by the "host-CLI-only" fence — categories
  that exist precisely because agents can, in principle, reach
  everything else.

This inversion — CLI and REST first, UI last — is why the two
embedded agents below can drive the product end-to-end.

## Embedded agents

Two chat agents are embedded in the product. Each operates with a
**delegated token** (§03) created from the calling user's session,
inheriting that user's full permissions. They share plumbing (client,
redaction, audit, approval) but differ in whose authority they carry.

### Owner/manager-side agent

Lives in the right sidebar (`.desk__agent`) of the owner/manager
desktop shell (§14). The sidebar is mounted once at the
`OwnerManagerLayout` level as a sibling of `<Outlet />`, so it
survives client-side route changes — the chat log scroll position,
the composer draft, and the `EventSource` subscription all persist
across navigation. New agent messages are delivered via the SSE event
`agent.message.appended`, so every connected tab sees them without
polling. Its tool surface is **the full CLI + REST surface available
to the delegating user** — every command the owner or manager can
execute in the UI or CLI is available to the agent. There is no
filtered capability catalog; tool descriptors are resolved
dynamically from the user's current role grants.

High-impact tools are routed through the approval pipeline (§
"Agent action approval" below). Two things can gate them:

- **Workspace policy** — committee-level actions (money routing,
  bulk destructives) go to the `/approvals` desk where a manager
  clicks "Approve" before execution.
- **Per-user agent approval mode** — the delegating user's own
  setting controls how eagerly the agent pauses for an inline
  confirmation card in the chat sidebar (`bypass | auto |
  strict`, default `strict`). The copy of each card is declared
  once on the API route as an `x-agent-confirm` annotation (§
  "Action confirmation annotation"), so CLI, REST middleware, and
  the chat UI all share the same wording.

The agent does **not** bypass its own approvals: when it proposes a
payroll issuance it still goes to the `/approvals` queue; when it
proposes an expense under an `auto`-mode user, a "Create expense
… for €22.10? [Confirm] [Reject]" card surfaces in that user's
chat and the agent waits for the tap. This costs one extra tap and
buys a canonical audit trail.

Default model: `google/gemma-4-31b-it`. Overridable via
`llm.assignments.set` under capability `chat.manager`.

Voice input is capability-gated (`voice.manager`); when on, audio
is transcribed via `voice.transcribe` before being dispatched to
the agent.

### Worker-side agent

Lives as the `Chat` tab in the worker PWA footer (§14). Its tool
surface is **the full CLI + REST surface available to the delegating
user** — every command the worker can execute is available to the
agent. Tool descriptors are resolved dynamically from the user's
current role grants and property assignments; the model sees only
tools the user is authorized to use.

Because the delegated token inherits the user's permissions, the
agent cannot read other users' data, cannot mutate other users' rows,
and cannot reach payroll or audit endpoints — those restrictions come
from the user's own access level, not from a filtered tool catalog.

Default model: `google/gemma-4-31b-it`. Capability keys
`chat.employee` (default on for workers) and `voice.employee`
(default off).

### Conversation compaction

Both agents accumulate long chat histories. Token budget grows with
thread length; "attention drag" on older, resolved topics degrades
answer quality. Every thread is therefore subject to **compaction**:

- The agent marks a **topic** as resolved when it has produced an
  accepted reply, a completed action, or an explicit "thanks" /
  dismissal from the human.
- Turns belonging to a resolved topic are compacted into a **short
  summary message** (one system-kind row) that replaces them in
  the live context window.
- The original, uncompacted turns are retained in the `chat_archive`
  table (scoped by workspace + thread) and remain **full-text
  searchable** from the agent's `search_chat_archive(q)` tool. The
  agent can therefore pull the original back into context on
  demand when a follow-up references an older topic.
- Compaction itself is an `llm_call` under capability
  `chat.compact`; the summary is verified against numeric claims
  the same way digests are (see "Daily digest / anomaly detection").

Compaction windows default to "30 days or 200 turns, whichever
first," overridable per workspace.

## Provider

- **Default:** OpenRouter — `https://openrouter.ai/api/v1/chat/
  completions`.
- **Default model:** `google/gemma-4-31b-it` (multimodal, per user).
- **Key** stored in `secret_envelope` (§15). Never logged. Never sent
  in audit JSON.
- Optional: a **secondary** provider (OpenAI-compatible URL + key) for
  fallback; the client round-robins on upstream 5xx.
- Local providers (Ollama, vLLM) are **out of scope for v1** but the
  provider adapter interface supports them.

## Client abstraction

```
class LLMClient(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[Message],
        images: list[ImageRef] = (),
        tools: list[ToolDef] | None = None,
        response_format: ResponseFormat | None = None,
        max_output_tokens: int | None = None,
        correlation_id: str,
        capability: Capability,
        budget: Budget | None = None,
    ) -> LLMResult: ...
```

`LLMResult` carries `text | tool_calls | structured`, `usage` (prompt
+ completion token counts, dollar estimate), `model_used`, and a
`finish_reason`.

## Capability catalog

Each feature names a **capability** key. The model assignment table
maps capability → model. If a capability has no explicit mapping, the
workspace default is used.

| capability key             | description                                                             |
|----------------------------|-------------------------------------------------------------------------|
| `tasks.nl_intake`          | Parse a free-text description into a task / template / schedule draft   |
| `tasks.assist`             | Staff chat assistant: "what's next?", explain an instruction, etc.      |
| `digest.manager`           | Morning owner/manager digest composition                                |
| `digest.employee`          | Morning worker digest composition                                       |
| `anomaly.detect`           | Compare recent completions to schedule and flag anomalies               |
| `expenses.autofill`        | OCR + structure a receipt image                                         |
| `instructions.draft`       | Suggest an instruction from a conversation with the owner/manager       |
| `issue.triage`             | Classify severity/category of a user-reported issue                     |
| `stay.summarize`           | Summarize a stay (for guest welcome blurb drafting)                     |
| `voice.transcribe`         | Turn a voice note into text (for chat assistant / issue reports)        |
| `chat.manager`             | Owner/manager-side embedded chat agent (§14 right sidebar)              |
| `chat.employee`            | Worker-side embedded chat agent (§14 Chat tab)                          |
| `chat.compact`             | Summarise resolved topics in a chat thread (see "Conversation compaction") |
| `chat.detect_language`     | Detect message language for auto-translation (§10, §18)                 |
| `chat.translate`           | Translate a message into the workspace default language (§10, §18)      |

## Model assignment

```
model_assignment
├── capability                 # key above; unique
├── provider                   # openrouter | other
├── model_id                   # e.g. google/gemma-4-31b-it
├── params_json                # temperature, top_p, etc.
├── budget_json                # per-call max tokens, per-day USD cap, per-min req cap
└── updated_at/updated_by
```

An owner or manager can edit this in the UI or via
`PUT /api/v1/llm/assignments/{capability}`. Default row for every
capability is seeded at install with Gemma 4 31B IT and sensible
params. Budgets are soft — the system warns and stops calls when
exceeded, with a clear message in the audit log.

### Capability defaults

| capability            | recommended default model                          | rationale |
|-----------------------|-----------------------------------------------------|-----------|
| all                   | `google/gemma-4-31b-it`                            | per user  |
| `expenses.autofill`   | `google/gemma-4-31b-it`                            | multimodal |
| `voice.transcribe`    | (none out of box; capability off unless assigned)  | local speech-to-text deferred |

Owners and managers may override any capability to a different model
(e.g. Claude Haiku for digests, a cheaper Qwen for intake) without
code changes.

## Prompting strategy

- **System prompts** are versioned files under `app/prompts/*.md` with a
  small Jinja2 header for injection. They are loaded once per process
  and hot-swappable in dev.
- **Schema-first outputs** wherever feasible: `response_format = json
  schema` via OpenRouter (Gemma supports JSON mode). Callers validate
  with Pydantic models and fail loudly on drift.
- **Grounding context** is assembled from the database and passed as
  structured tool observations or system-message content, never as
  free text inside user messages.
- **Few-shot** for stable shapes (expense OCR, task intake) committed
  as fixtures; `pytest` regressions run against them.

## Redaction / PII

A redaction layer sits between the domain and the `LLMClient`:

- `email`, `phone_e164`, `full_legal_name` → tokenized with a salted
  hash substitution (stable within a call).
- Addresses truncated to city.
- Access codes, wifi passwords, bank numbers: hard-drop.
- Free-text fields (notes, descriptions) pass through
  **regex+NER scrub**: emails, phone-like patterns, IBAN patterns.
- Household can turn on **strict** mode, which adds a small local
  classifier step (deferred to a plug-in; not in v1 critical path).

Every `llm_call` row stores both the **redacted** payload sent and
the response received. Original values are never stored on `llm_call`.
Retention: 90 days by default, configurable per workspace (§02).

## Agent audit trail

Every write performed via a delegated token is captured in `audit_log`
(§02) and attributed to the **delegating user**, not to a separate
"agent" actor:

- `actor_kind` = `user` (all human actors now use a single kind; see
  §02).
- `actor_id` = the delegating user's ULID.
- `actor_grant_role` = the highest grant_role the user held at the
  time of the action (e.g. `manager`, `owner`); denormalized for
  display.
- `via` = `api` or `cli`.
- `token_id` = the delegated token's id (join to `api_token` for
  delegation metadata).
- `agent_label` = the token's `name` field, denormalized for display
  (e.g. "manager-chat-agent"). Set only for delegated tokens.
- `agent_conversation_ref` = from the `X-Agent-Conversation-Ref`
  header — an opaque reference (up to 500 chars) linking the audit
  entry back to the conversation or prompt that triggered the action.
- `reason` = from `X-Agent-Reason` header (free text, up to 500 chars,
  as before).
- `correlation_id` propagated from `X-Correlation-Id` if present, else
  generated server-side and returned via `X-Correlation-Id-Echo`.

The owner/manager's **Agent Activity** view filters `audit_log` by
`actor_kind = 'agent' OR agent_label IS NOT NULL` — capturing both
standalone scoped-token agents and delegated-token agents — with
facets on token, action, and time range, and a line chart of call
volume per token. Because `actor_id` points to the human, every agent
action is also visible in the user's own audit trail.

## Agent action approval

Two independent layers of gating protect agent-initiated writes:

- **Workspace policy (committee).** The workspace's owner/manager
  curates a list of actions that require a **manager to click
  "Approve" in `/approvals`** before they commit — a
  committee-style decision on workspace-wide safety (money
  routing, bulk destructives, engagement-kind changes crossing
  payroll). This layer applies to every non-passkey writer
  (scoped and delegated tokens alike) and the "always-gated"
  items inside it cannot be disabled from the UI. See "Workspace
  policy: which actions" below.
- **Per-user agent approval mode (self).** On top of the
  workspace layer, every user carries a personal mode
  (`bypass | auto | strict`) that decides when **the user's own
  embedded chat agent pauses to show a confirmation card in the
  same chat channel** before executing. The card says things like
  "Create expense *Groceries Marché Provence* for €22.10?
  [Confirm] [Reject]". The per-action copy of the card is
  declared on the OpenAPI route (see "Action confirmation
  annotation" below), so the same text is used by the REST
  middleware, the CLI, and the chat UI without duplication. This
  layer never narrows the workspace policy; it only adds inline
  self-confirmations on top.

The two layers are complementary: an action in the workspace
always-gated list always goes to `/approvals` regardless of the
user's mode; an action that only carries an `x-agent-confirm`
annotation goes to the user's chat channel when their mode is
`auto` or `strict`; an action with neither still surfaces in chat
under `strict`. Reads are never gated by either layer.

All gates — workspace and self — produce the same `agent_action`
rows. They appear together in the `/approvals` desk for
owner/manager oversight and, when the triggering channel supports
it, are rendered inline in the chat surface where the agent lives
(see "Inline approval UX").

### Action confirmation annotation

Instead of maintaining a separate "approvable actions" list per
surface, every mutating route in §12 may carry an OpenAPI
extension declaring its inline-confirmation copy and metadata:

```yaml
# e.g. POST /api/v1/expenses
x-agent-confirm:
  summary: "Create expense {vendor} for {amount_minor|money:currency}?"
  risk: medium
  fields_to_show: [vendor, amount_minor, currency, property_id, category]
  verb: "Create expense"
```

- `summary` — one-line template rendered against the resolved
  request payload. The placeholder syntax matches the i18n seam
  (§18); `|money:<currency-key>` is the one built-in filter in
  v1 so expense cards render as `€22.10` and not `2210`. Authors
  can mix payload keys with server-resolved fields (e.g. the
  resolved property name) without leaking unpromoted internals.
- `risk` — `low | medium | high`; drives the card's tone and, for
  `high`, forces the "Details" pane open by default.
- `fields_to_show` — ordered list of payload keys rendered as a
  compact key/value table under the summary.
- `verb` — short label for logs and audit, defaults to the
  operation's `summary` when absent.

An endpoint that omits `x-agent-confirm` is considered not to
need inline confirmation. It executes silently under `auto` and
surfaces a generic "Run `{operation_id}` with these fields?" card
only under `strict`.

The annotation is the **single source of truth** for confirmation
copy. The CLI `_surface.json` exposes it alongside `x-cli`
(§13); the REST middleware reads it at request time; the chat UI
renders the same summary without a second copy of the strings.
A CI lint (§17) flags any mutating route whose `x-agent-confirm`
references a payload key that does not exist on the route's
request model, so the cards never show blank values.

A small starter list of routes that carry
`x-agent-confirm` in v1:

| route                                        | summary template                                                    |
|----------------------------------------------|---------------------------------------------------------------------|
| `POST /tasks`                                 | "Create task *{title}* at {property_id|property:name} on {when}?"    |
| `POST /tasks/{id}/assign`                     | "Assign *{task_id|task:title}* to {user_id|user:display_name}?"      |
| `POST /tasks/{id}/complete`                   | "Mark *{task_id|task:title}* complete?"                              |
| `POST /expenses`                              | "Create expense *{vendor}* for {amount_minor|money:currency}?"      |
| `POST /issues`                                | "Report *{title}* at {property_id|property:name}?"                  |
| `POST /inventory/{id}/restock`                | "Restock *{id|inventory:name}* by {qty} {unit}?"                     |
| `POST /schedules`                             | "Add schedule *{template_id|template:name}* on {rrule}?"             |
| `POST /stays`                                 | "Create stay at {property_id|property:name} {check_in}–{check_out}?" |
| `POST /messaging/broadcast` (single-recipient path) | "Message {recipient_user_id|user:display_name}: *{subject}*?" |

Routes not in this starter list (and not in the workspace policy
lists) execute silently in `auto` mode; the list grows
surgically per surface, not at the annotation layer.

### Per-user agent approval mode

Every `users` row carries `agent_approval_mode`, an enum the user
sets on their own profile (§14). It decides when the user's own
embedded chat agent (§ "Embedded agents") pauses for an inline
confirmation card before executing a **mutating** delegated-token
request.

| mode     | `x-agent-confirm` on the route? | no annotation, mutating |
|----------|---------------------------------|--------------------------|
| `bypass` | execute silently                | execute silently         |
| `auto`   | show inline confirmation card using the annotation's `summary` / `fields_to_show` / `risk` | execute silently |
| `strict` | show inline confirmation card using the annotation | show generic card (`verb` + full payload) |

Reads and `--dry-run` / `--explain` invocations (§13) always
execute silently — approval fatigue is the enemy of a working
safety rail.

**Defaults.** New `users` rows are seeded at `strict`; on-boarding
walks the user through the three choices so the first agent
interaction is never a surprise. Mode changes are a **per-user
decision** — the user changes it on their own profile, and no
other user (not even an owner) may change it for them. Every
change writes `auth.agent_mode_changed` to `audit_log` (§02) so
oversight remains possible via the audit surface.

**Scope.** The mode applies **only** to requests authenticated by
a delegated token (§03) whose `delegate_for_user_id` equals this
user's id. Passkey sessions, scoped API tokens, host-CLI runs
(§13), and other users' delegated tokens are unaffected. In
short: the gate fires for "my embedded chat agent acting as me"
and nothing else. External CLI and REST callers are unchanged.

**Workspace policy still wins.** `bypass` does **not** weaken the
workspace always-gated list. Those rows still land in
`/approvals`, still require a manager click, and are immovable.
The per-user mode is additive only — on actions the workspace
does not already gate.

### Workspace policy: which actions

The canonical list, configurable per workspace:

- Any `*.delete` that would affect more than **10 rows**.
- Work engagement archive (`work_engagement.archive`).
- Payslip issuance and paid transition (`payroll.issue`, `payroll.pay`).
- Granting a new scope to an existing token.
- Rotating another token.
- Sending a broadcast email to more than one recipient.
- Bulk schedule changes affecting > 50 future tasks.

**Always-gated (not configurable)** — these actions touch money
routing; the approval requirement cannot be disabled in
`/settings/approvals`:

- `payout_destination.create`
- `payout_destination.update`
- `payout_destination.archive` (when the row is currently referenced
  as a default)
- `work_engagement.set_default_pay_destination`
- `work_engagement.set_default_reimbursement_destination`
- `expense_claim.set_destination_override` (agent path; the owner or
  manager selecting a destination in the approval UI is itself the
  approval)
- `work_order.accept_quote` (§22) — commits the workspace to a
  price
- `vendor_invoice.approve` (§22) — commits payment routing (the
  agent path mirrors `expense_claim.set_destination_override`: the
  owner or manager selecting a destination in the approval UI is the
  approval)
- `vendor_invoice.mark_paid` (§22) — commits the `approved → paid`
  transition
- `organization.update_default_pay_destination` (§22) — routes
  future agency-supplied invoices by default
- `work_engagement.set_engagement_kind` (§05) — **only** when the
  transition crosses the `payroll` boundary (to or from), because it
  moves the worker between pay pipelines. `contractor ↔
  agency_supplied` is owner/manager-only but not agent-approval-gated.

### Interactive-session-only endpoints

A separate, stricter class of HTTP endpoints requires a **live
passkey session** — they refuse all bearer tokens (whether scoped or
delegated) and return `403 forbidden` with
`WWW-Authenticate: error="session_only_endpoint"`. The approval
middleware does **not** write an `agent_action` row for these,
because doing so would itself be the leak: the middleware persists
`resolved_payload_json` and (on execution) `result_json`, and for
these endpoints the response contains decrypted secret material that
must never land in a persisted row.

v1 members of the list:

- `POST /payslips/{id}/payout_manifest` — full decrypted account
  numbers for treasury use (§09).

These endpoints are **manager-session only**: they require a logged-
in manager passkey session, not any bearer token. The idempotency
cache (§12) explicitly does **not** persist their responses — a
replay re-executes against the current secret store and re-audits,
rather than serving a cached body.

### Host-CLI-only administrative commands

A related but distinct class: administrative commands that have
**no HTTP surface at all**, agent or human. They are invoked only
via `miployees admin <verb>` on the deployment host, with shell
access to the running service's environment. This is a stronger
boundary than interactive-session-only — there is literally no network path to
them, so the approval system does not apply and the idempotency
cache does not exist for them.

v1 members:

- `miployees admin rotate-root-key` — envelope-key rotation (§15).
- `miployees admin recover` — offline lockout magic-link issuance
  (§03).
- `miployees admin purge` — hard-delete per-person payload (§02,
  §15).

The agent-approval flow (§11) does not apply here because there is
no request for the middleware to intercept. The operator audits
these commands via shell history, the on-host `audit_log` rows each
command writes directly, and deployment-level controls on who can
`docker compose exec` into the container.

### Flow

1. Agent calls the endpoint normally.
2. Middleware resolves whether to gate, in order:
   - action in the workspace **always-gated** list → gate, source
     `workspace_always`, destination `/approvals` desk;
   - action in the workspace **configurable** list → gate, source
     `workspace_configurable`, destination `/approvals` desk;
   - token is delegated AND user mode is `auto` AND the route
     carries `x-agent-confirm` → gate, source
     `user_auto_annotation`, destination user's inline chat;
   - token is delegated AND user mode is `strict` AND the action
     is mutating → gate, source `user_strict_mutation`,
     destination user's inline chat (the card uses
     `x-agent-confirm` when present, falls back to a generic
     `{verb} with these fields?` template otherwise);
   - otherwise → execute.
3. On gate, the middleware writes an `agent_action` row with the
   fully-resolved request payload (including idempotency key),
   `pre_approval_source`, `inline_channel` (from `X-Agent-Channel`
   if present, else `desk_only`), `for_user_id`, and a snapshot of
   `resolved_user_mode`.
4. Returns `202 Accepted` with
   `{ "approval_id": "appr_…", "status": "pending", "expires_at": "…" }`.
5. Deciders are notified:
    - **Inline** — for `inline_channel` in `{web_owner_sidebar,
      web_worker_chat}`, the `agent.action.pending` SSE event is
      pushed to the delegating user's tabs and the chat surface
      renders an approval card.
    - **Desk** — owners and managers receive the existing email +
      `approval.pending` webhook; the row is visible on
      `/approvals`.
6. Any authorised decider (the delegating user in their own inline
   chat, or any owner/manager in `/approvals`) approves or rejects.
7. On approval, the original handler is invoked with the recorded
   payload; result is stored. Agent polls `GET /api/v1/approvals/
   {id}` (or receives `approval.decided` webhook) and proceeds.
8. If `expires_at` passes without decision, status becomes
   `expired`.

### Model

```
agent_action
├── id
├── approval_id                # human-shown
├── requested_at
├── requested_by_token_id
├── for_user_id                # users.id — delegating user; null for scoped-token requests
├── correlation_id
├── action                     # dotted verb (operationId)
├── resolved_payload_json      # full resolved URL, method, body
├── idempotency_key
├── state                      # pending | approved | rejected | expired | executed
├── gate_source                # workspace_always | workspace_configurable | user_auto_annotation | user_strict_mutation
├── gate_destination           # desk | inline_chat
├── card_summary               # rendered `x-agent-confirm.summary` at request time (authoritative for inline)
├── card_risk                  # low | medium | high — from annotation, else derived from gate_source
├── card_fields_json           # resolved {key: display_value} map from `fields_to_show`
├── inline_channel             # desk_only | web_owner_sidebar | web_worker_chat | offapp_whatsapp | offapp_sms
├── resolved_user_mode         # bypass | auto | strict — snapshot of the delegating user's mode at request time; null if no delegated token
├── decided_at
├── decided_by_user_id
├── decision_note_md
├── executed_at
└── result_json
```

The `card_*` fields are **rendered at request time** and stored,
so later renders — or a /approvals desk fetch — always show the
same copy even if the template, the referenced row, or the user's
locale has changed since. The middleware templates `summary`
using the resolved payload plus the i18n filters in §18.

### Bypass

Neither a scoped token carrying `admin:*` scope nor a delegated token
can bypass the workspace policy on default-approvable actions. The
only way to disable workspace-level gating on a configurable action
is for an owner or manager to flip the workspace-level setting in
`/settings/approvals`; always-gated actions never disable.

The per-user `bypass` mode (§ "Per-user agent approval mode") is
distinct and does **not** touch workspace policy. It declares that
the user adds no further gates on top of workspace policy for their
own delegated-token writes — it cannot remove what the workspace
already requires.

### Inline approval UX

When a gated action originates from an embedded chat agent, the
`agent_action` row is annotated with the chat channel that
triggered it. The agent's HTTP request carries an
**`X-Agent-Channel`** header; accepted values for v1:

| value                 | channel                                                |
|-----------------------|--------------------------------------------------------|
| `web_owner_sidebar`   | Owner/manager desktop sidebar chat (§14 `.desk__agent`) |
| `web_worker_chat`     | Worker PWA Chat tab (§14)                              |
| `offapp_whatsapp`     | WhatsApp thread (§10 `preferred_offapp_channel`)        |
| `offapp_sms`          | SMS thread (§10)                                       |
| *absent*              | `desk_only` — approval appears only in `/approvals`    |

For the two web channels, pending approvals are pushed to the
delegating user's open tabs over SSE via
`agent.action.pending` (scoped to `for_user_id`), and the chat
surface renders an approval card with **Approve** / **Reject**
buttons wired to the same `/approvals/{id}/{decision}` endpoints
that the desk uses. The same row remains visible on `/approvals`
so owners and managers can oversee agent activity across users.

The off-app channel values are named here for schema stability —
`agent_action.inline_channel` accepts them and audit surfaces flag
them — but the v1 implementation delivers inline approvals only
through the two web channels and the `/approvals` desk. The
WhatsApp / SMS reply-based flow (send a one-tap confirmation
message, parse `YES` / `NO` from the reply, defeat replay of the
same message id) is tracked under §19 "Beyond v1".

### TTL

`expires_at` defaults to **7 days** from `requested_at`. Per-action
overrides are allowed (some workspaces may want shorter windows for
sensitive actions like `payroll.pay`). When `expires_at` passes
without a decision, a worker flips `state` to `expired`, emits
`approval.decided` with `decision = expired`, and records
`decision_note_md = "auto-expired"`. Expired approvals cannot be
revived — the agent must re-request.

## Natural-language task intake

`POST /api/v1/tasks/from_nl`:

```json
{ "text": "Have Maria deep-clean the guest bath every Tuesday 9am at Villa Sud, 1 hour, needs photo evidence", "dry_run": true }
```

Response:

```json
{
  "preview_id": "nlp_…",
  "resolved": {
    "property_id": "prop_…",
    "assigned_user_id": "usr_…",
    "template": {…},
    "schedule": { "rrule": "FREQ=WEEKLY;BYDAY=TU", … }
  },
  "assumptions": [
    "Assumed Villa Sud (only match).",
    "Resolved Maria to usr_…",
    "Photo evidence flagged because 'needs photo evidence' was mentioned."
  ],
  "ambiguities": []
}
```

If `ambiguities` is non-empty, the caller is expected to `POST
/from_nl/commit` with `resolved` patched or pick an alternative.

Commit endpoint honors `Idempotency-Key`.

## Daily digest / anomaly detection

Cron job composes markdown digests per recipient from structured DB
queries, then the LLM writes the prose wrapper. The digest body's
**structured data** (list of overdue tasks, upcoming stays) is
authoritative; the LLM is only allowed to summarize, not to
contradict. A post-generation check compares numeric claims to the
source data and rewrites on mismatch.

Anomaly detection query (simplified):

- Tasks scheduled but no matching completion in the window → candidate
  anomaly.
- Sudden drop in a user's completion rate vs 4-week baseline.
- Inventory consumption deviating > 3σ from rolling mean.

The LLM ranks candidates and writes a one-line explanation per
anomaly. Falsely-flagged items can be "Ignore — stop suggesting this",
persisted in the `anomaly_suppression` table.

### `anomaly_suppression`

| field            | type     | notes                                   |
|------------------|----------|-----------------------------------------|
| id               | ULID PK  |                                         |
| workspace_id     | ULID FK  |                                         |
| anomaly_kind     | text     | e.g. `task_missed`, `completion_rate_drop`, `consumption_spike` |
| subject_kind     | text     | `task_template` \| `user` \| `inventory_item` \| ... |
| subject_id       | ULID     |                                         |
| suppressed_until | tstz     | **required** — the UI forces the owner/manager to pick an explicit window when suppressing |
| reason           | text?    | free-form, shown in the digest once the suppression expires |
| suppressed_by    | ULID FK  | user_id of owner or manager who suppressed |
| created_at       | tstz     |                                         |

Scope is `(workspace_id, anomaly_kind, subject_id)`. Permanent
suppression is not offered: chronic "false positives" usually stop
being false when the underlying pattern shifts, and a forced revisit
keeps the digest honest. An owner or manager who wants a long
suppression enters a correspondingly long `suppressed_until` — the
UI defaults to 90 days, accepts any future date.

## Staff chat assistant

For users with `chat.assistant` capability on (default on for workers).

- Available as a bottom-nav chat bubble on the PWA.
- Tools exposed to the assistant (subset of the REST API, scoped to
  the current user):
    - `get_tasks_today()`, `mark_task_done(task_id)`,
      `report_issue(area, description)`, `get_instruction(id)`,
      `start_shift()`, `end_shift()`, `get_inventory_low()`.
- Voice input uses `voice.transcribe` capability; disabled by default.
- Never fabricates tasks: the assistant cannot create arbitrary rows,
  only invoke the exposed tools.

## Cost tracking

Every LLM call writes to `llm_call` with the provider's reported
`usage.total_tokens` and an estimated USD cost from a small pricing
table kept in config. The background worker aggregates daily totals
and the owner/manager dashboard shows rolling 30-day LLM spend, per
capability and per model. Exceeding a configured daily cap disables
the capability for the rest of the day (soft-fail: humans still work;
agents see a clear error).

## Failure modes

- Provider 5xx: one retry after jitter, then fail the caller;
  capability degrades gracefully (autofill blank, digest skipped,
  etc.).
- Rate-limited: honor `Retry-After`; callers see 503 with a
  `Retry-After` header.
- Content refused / unsafe: return an empty structured output; log
  `finish_reason`; caller surfaces a neutral fallback.
- Budget exceeded: 429 with explanation; capability paused until next
  day.

## Out of scope (v1)

- Fine-tuning or prompt-caching beyond what OpenRouter offers
  natively.
- Retrieval-augmented generation across the whole DB (we pick
  relevant rows per capability).
- Autonomous long-running agent loops hosted in-process. Agents run
  elsewhere and call the API.
