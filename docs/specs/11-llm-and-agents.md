# 11 — LLM integration and agents

Per the user's direction: **Google Gemma 4 31B IT via OpenRouter**
(`google/gemma-4-31b-it`) is the default model, with a per-capability
assignment table so other models can substitute for specific jobs.
All in-app agentic features (natural-language task intake, daily
digest, anomaly detection, receipt OCR, staff chat assistant, agent
audit trail, action approval, embedded owner/manager and worker chat
agents) share the same plumbing.

## The agent-first invariant

crewday is built around a hard rule: **every human UI verb exists
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

Both agents are reached through the in-app chat surfaces that ship in
v1: on desktop, the right-hand `.desk__agent` sidebar (shared between
the worker and owner/manager shells, see §14 "Desktop shell"); on
mobile, a bottom-bar entry — the worker shell's `Chat` tab navigates
to the full-screen `/chat` page, and the manager shell's
`.desk__bottom-dock` opens `.desk__agent` as an off-canvas drawer.
§23 keeps the off-app gateway design on the shelf for later, but
external channels are not enabled in shipped v1. The agent code path
is therefore channel-agnostic in principle, but the only live
transports are the web surfaces above.

### Owner/manager-side agent

Lives in the right sidebar (`.desk__agent`) of the owner/manager
desktop shell (§14) — the same shared component that the worker
desktop shell mounts on its right edge. The sidebar is mounted once
at the `ManagerLayout` level as a sibling of `<Outlet />`, so it
survives client-side route changes — the chat log scroll position,
the composer draft, and the `EventSource` subscription all persist
across navigation. On mobile, the manager shell renders a single
bottom dock button (`.desk__bottom-dock`) that opens the same sidebar
as an off-canvas right drawer. New agent messages are delivered via
the SSE event `agent.message.appended`, so every connected tab sees
them without polling. Its tool surface is **the full CLI + REST surface available
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

On mobile, lives behind the `Chat` tab in the worker PWA footer
(§14), which navigates to the full-screen `/chat` page. On desktop,
lives in the right-hand `.desk__agent` rail of the worker shell —
the same shared component that the manager shell mounts (§14
"Desktop shell"); a `role` prop on `AgentSidebar` selects the
per-role agent log/message endpoints
(`/api/v1/agent/{employee|manager}/{log,message}`) and gates the
manager-only "Pending approvals" block out of the worker view. Its
tool surface is **the full CLI + REST surface available to the
delegating user** — every command the worker can execute is
available to the agent. Tool descriptors are resolved dynamically
from the user's current role grants and property assignments; the
model sees only tools the user is authorized to use.

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

## Agent preferences

A seam for free-form, human-authored guidance that shapes how
agents talk back — the crewday analogue of stacked `CLAUDE.md`
files. Preferences are **soft** directives: they live alongside
the structured §02 settings cascade (hard rules) and feed into
the same LLM turns that the settings cascade already constrains.

Examples an owner, manager, or worker might write:

- Workspace: "We bill in euros. Always show amounts as `€1 234,56`
  even when a property's currency is different."
- Property (Villa Sud): "Gardener comes Tuesdays; never propose
  outdoor tasks that day."
- Self: "Don't request a photo on my tasks unless I ask. Keep
  replies to one paragraph."

### Layers

Three layers, in the order the model sees them (broadest first):

1. **Workspace** — one blob per workspace. Applies to every
   delegated-token turn in the workspace plus workspace-scoped
   composition capabilities (digests, anomaly phrasing,
   NL-intake drafts).
2. **Property** — one blob per property. Injected per the
   **context resolution** rule below.
3. **User** — one blob per (user, workspace). Applies to every
   delegated-token turn where the delegating user is this user,
   across every channel of the chat gateway (§23). Self-writable
   only; no one else — not even an owner — may edit another
   user's preference blob (mirrors the "self-writable" rule on
   `agent_approval_mode`).

Each layer is a Markdown document. The model receives the three
layers as three clearly labelled sections of the system prompt:

```
## Workspace preferences — Bernard workspace
<workspace blob, verbatim>

## Property preferences — Villa Sud
<property blob, verbatim>

## Your preferences — Jean B.
<user blob, verbatim>
```

The label includes the scope's display name so the model can
attribute and reconcile conflicts the same way a human would —
"later/more-specific wins" is the convention, not a hard rule
carved into tool code.

### Which capabilities receive preferences

Composition and conversation capabilities only. The resolver
injects the stack into system prompts for:

- `chat.manager`, `chat.employee`, `chat.compact`
- `digest.manager`, `digest.employee`
- `tasks.nl_intake`, `tasks.assist`
- `instructions.draft`, `stay.summarize`, `issue.triage`

Classification, OCR, and detection capabilities do **not** receive
preferences — they would be noise at best and harmful at worst:
`expenses.autofill`, `voice.transcribe`, `anomaly.detect`,
`chat.detect_language`, `chat.translate`. The capability catalog
flag `receives_agent_preferences: true` on `model_assignment`
makes this explicit and lets a workspace toggle the default for a
given capability.

### Property context resolution

A manager may hold grants on many properties in the same turn; a
worker typically touches one or two per shift. The resolver picks
which property blob(s) to inject per turn by walking the
following rules, stopping at the first that yields an answer:

1. **Explicit context.** The chat gateway's current thread (§23
   `chat_thread.primary_property_id` when set by an
   `X-Agent-Channel`-bound UI) names one property — inject
   only that one's blob.
2. **Single reachable property.** The delegating user holds
   grants on exactly one property in the workspace — inject
   that one.
3. **Multiple reachable properties.** Inject every property
   blob the user can reach as labelled sections ("## Property
   preferences — Villa Sud", "## Property preferences — Mas
   des Oliviers"), followed by a one-line resolver note:
   "Multiple properties in scope; confirm with the user if the
   answer depends on which property they mean." The model
   picks or asks.
4. **No property reachable.** No property blob is injected;
   workspace + user blobs still flow normally.

Rule 3 is capped by the size budget below. If the concatenated
property blobs would exceed the cap, the resolver drops all
property blobs and emits a system note: "Multiple properties in
scope; call `get_agent_preferences(property_id)` to pull a
specific property's preferences." The tool is registered on
every capability in the receive-list above with the same
delegated-token scope as the rest of the agent's surface.

### Authoring and visibility

Authoring is gated by three new keys in the §05 action catalog:

- `agent_prefs.edit_workspace` — `default_allow: owners,
  managers`; valid on `workspace`.
- `agent_prefs.edit_property` — `default_allow: owners,
  managers`; valid on `workspace, property`.
- `agent_prefs.edit_self` — identity-scoped; self-writable only,
  not in the action catalog.

There is no separate read action key. **Anyone with a grant on
the scope may read that scope's workspace or property
preferences via REST or CLI**, because the blob is what shapes
the agent they are talking to and transparency builds trust in
agent behaviour. The UI, however, only surfaces the editor
**and the full body** to users who pass the corresponding edit
key — viewers without write access see a short notice
("preferences are set by your manager; read the full text via
`crewday agent-prefs show workspace`") rather than the raw
Markdown, to keep the settings page from doubling as a leak
surface for casual observers. User-layer preferences are
private to their author; no one else may read another user's
self blob.

### PII posture

Preferences are **pass-through**. The §11 redaction layer that
scrubs names, phones, emails, and addresses from other free-text
inputs is **skipped** for preference text — the whole point is
to reference real people and real places ("don't pair Maria
with the night shift at Villa Sud"), and scrubbing would turn
the blob into nonsense.

To keep the carve-out narrow, the save endpoint refuses
preference bodies that match hard-drop secret patterns already
called out in §11 and §15:

- IBAN-shaped tokens, bank-account-shaped numbers
- Access codes, door codes, alarm codes (regex + heuristic)
- Wi-Fi passwords (heuristic; keyword-triggered)
- API tokens (`mip_…`), envelope keys, OAuth bearers

A save that matches returns `422` with
`error = "preference_contains_secret"` and a pointer to the
offending span. The UI surfaces a banner above the editor:
"Preferences are sent to the model as written. Do not paste
passwords, codes, or account numbers." This sits next to the
existing §15 PII notice, not in place of it.

### Size budget

Each layer carries a soft cap of **4 000 tokens** (measured with
the default model's tokenizer at save time) and a hard cap of
**16 000 tokens**. The editor shows a live counter computed
client-side with a BPE approximation (`gpt-tokenizer`'s
`o200k_base`, which agrees with Gemma's SentencePiece to within
a few percent on typical prose) — it is **advisory only**. The
server's save-time count is authoritative; if the two disagree
on a value near the cap, the server's 422 wins. Save past the
hard cap returns `422 preference_too_large`. Combined
injection budget per turn is capped at **8 000 tokens**; when
the concatenated stack exceeds this, the resolver drops property
blobs first (per "Property context resolution" rule 3), then
truncates the workspace blob from the end with a "[truncated]"
marker. User blobs are never truncated — they are the smallest
layer in practice and the most personal, so the UX is worse when
they disappear silently.

### Storage, versioning, audit

One row per (scope_kind, scope_id) in `agent_preference` (§02
"Shared tables"). Every save writes a new `agent_preference_revision`
row, keeping full history; the resolver always reads the latest
non-archived row. Edits emit `audit_log` entries
(`agent_preference.updated`) and the webhook family
`agent_preference.*` (§10). Retention follows the workspace's
`retention.audit_days` setting; revision bodies older than 2
years are pruned with the corresponding audit row.

### Not a substitute for structured rules

Preferences are advice, not enforcement. "Don't request a photo
on my tasks" expressed here will bias the agent's prose but does
**not** override `evidence.policy = require` from the §02
settings cascade — an agent that tries to complete a task
without a photo still fails the server-side check. For hard
rules, use the settings cascade; for soft rules the agent
should carry into its phrasing and its proposals, use
preferences. The §14 editor explains this directly above the
textarea.

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
via `crewday admin <verb>` on the deployment host, with shell
access to the running service's environment. This is a stronger
boundary than interactive-session-only — there is literally no network path to
them, so the approval system does not apply and the idempotency
cache does not exist for them.

v1 members:

- `crewday admin rotate-root-key` — envelope-key rotation (§15).
- `crewday admin recover` — offline lockout magic-link issuance
  (§03).
- `crewday admin purge` — hard-delete per-person payload (§02,
  §15).
- `crewday admin budget set-cap` — adjust a workspace's rolling
  30-day agent usage cap (§ "Workspace usage budget"). Usage:
  `crewday admin budget set-cap --workspace <id> --cap-usd <value>
  [--note "<reason>"]`. Writes the new `cap_usd_30d` value and a
  `workspace_budget.updated` `audit_log` row with `actor_kind =
  'system'` and `via = 'cli'`. No HTTP surface by design — the
  operator is the only principal that can commit the workspace to a
  larger spend.
- `crewday admin budget show` — prints the current cap, the rolling
  30-day cost, and the percent used for one workspace or all of
  them.
- `crewday admin budget reload-pricing` — re-reads
  `app/config/llm_pricing.yml` without restarting the process. Used
  after bumping per-1k-token prices in a config change.

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
├── inline_channel             # desk_only | web_owner_sidebar | web_worker_chat | offapp_whatsapp
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
| `web_owner_sidebar`   | Owner/manager web client — `.desk__agent` (desktop) or its mobile bottom-dock drawer (§14 "Desktop shell") |
| `web_worker_chat`     | Worker web client — `.desk__agent` (desktop) or full-screen `/chat` (mobile, opened from the bottom-nav `Chat` tab) (§14) |
| `offapp_whatsapp`     | Reserved for a future WhatsApp adapter (§23, deferred) |
| *absent*              | `desk_only` — approval appears only in `/approvals`    |

Channel values are role-scoped, not viewport-scoped: a single SPA
bundle ships per role, and the chat surface picks its presentation
(desktop sidebar vs. mobile drawer / full-screen) from CSS. The SSE
socket and the `agent.action.pending` payload are identical across
the two presentations.

For the two web channels, pending approvals are pushed to the
delegating user's open tabs over SSE via
`agent.action.pending` (scoped to `for_user_id`), and the chat
surface renders an approval card with **Approve** / **Reject**
buttons wired to the same `/approvals/{id}/{decision}` endpoints
that the desk uses. The same row remains visible on `/approvals`
so owners and managers can oversee agent activity across users.

`offapp_whatsapp` remains **deferred**. Its value stays in the
schema so the approval pipeline does not need a later re-design,
but shipped v1 only renders inline cards in the two web surfaces.
See §23 for the deferred transport-specific behaviour.

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

For users whose workspace / work-engagement settings enable chat.

- Available as a bottom-nav chat bubble on the PWA.
- Tools exposed to the assistant (subset of the REST API, scoped to
  the current user):
    - `get_tasks_today()`, `mark_task_done(task_id)`,
      `report_issue(area, description)`, `get_instruction(id)`,
      `start_shift()`, `end_shift()`, `get_inventory_low()`.
- Voice input uses the `voice.enabled` setting plus the
  `voice.transcribe` model assignment; disabled by default.
- Never fabricates tasks: the assistant cannot create arbitrary rows,
  only invoke the exposed tools.

## Cost tracking

Every LLM call writes to `llm_call` with the provider's reported
`usage.total_tokens` and an estimated USD cost from the **pricing
table** (§ "Pricing table" below). The background worker aggregates
these rows into the rolling meter used by the **workspace usage
budget** (§ "Workspace usage budget" below) and into the per-capability
daily breakdowns on the LLM settings page. The per-capability
`model_assignment.budget_json` caps (per-call max tokens, per-day
USD, per-minute reqs) are enforced in the client; the workspace
rolling cap is enforced before that, as an envelope over every
capability.

## Workspace usage budget

One layer above the existing per-capability `model_assignment.budget_json`
caps: a **workspace-wide rolling dollar budget** that envelopes every
LLM capability charged to the workspace. The per-capability caps stay
— they remain useful to throttle a single noisy capability — but the
product-level question "how much is this household spending on agents?"
is answered by this envelope, and so is the "stop me before I
overspend" guard.

### Meter

Window: **rolling 30 days**. There is no calendar alignment, no
monthly reset, and no reset date shown to the user. The user-visible
label is always "Rolling 30 days"; the implementation is a trailing-
window sum over `llm_call.cost_usd` scoped by `workspace_id` and
`at >= now() - interval '30 days'`.

- `workspace_usage.cost_30d_usd` is a **materialized aggregate**
  refreshed by the worker every 60 s (cheap query) and recomputed
  from `llm_call` on process start. A pre-flight check on every LLM
  call adds the projected cost of the current call (estimated from
  `prompt_tokens + max_output_tokens`) to the cached aggregate
  before comparing to the cap; the post-call update uses the
  provider's actual `usage.total_tokens`.
- Rows older than 30 days are ignored by the meter regardless of
  retention settings (§15 keeps `llm_call` for 90 days for audit
  reasons; only the first 30 contribute to the meter).

### Cap

Stored on the workspace:

```
workspace_budget
├── workspace_id          ULID PK/FK
├── cap_usd_30d           numeric(8,4)  -- e.g. 5.0000
├── set_by                text           -- 'default' | 'operator'
├── set_at                tstz
└── note                  text?          -- operator-supplied justification
```

Defaults:

- **Prod:** `cap_usd_30d = 5.0000` seeded at workspace creation. This
  row is inserted in the same transaction as the workspace row.
- **Demo:** `cap_usd_30d = 0.1000` seeded per scenario (§24); see
  "Demo mode overrides" below.

Raising or lowering the cap has **no HTTP surface**. It is a host-CLI
admin command (§16 `crewday admin budget set-cap`). The rationale
mirrors the §11 "Host-CLI-only administrative commands" pattern: the
operator is the only principal that can commit their billing to a
larger spend, and that commitment belongs on the host, not in the app.
An owner or manager who feels they need more budget contacts the
operator out-of-band.

The UI exposes neither dollar amounts nor the cap itself to any
grant role. See "Visible surfaces" below.

### At-cap behaviour

A call is **refused** when `cost_30d_usd + projected_call_cost >
cap_usd_30d`. The refusal is first-class and structured:

```json
{
  "error": "budget_exceeded",
  "capability": "chat.manager",
  "window": "30d_rolling",
  "message": "Workspace agent budget exceeded. Agents will resume as older calls age out."
}
```

- Capability callers (chat composers, the digest job, NL intake) surface
  the message as-is; the agents render a neutral banner in the chat
  surface and do not attempt a retry.
- The `llm_call` row is **not** written for a refused call — the
  meter counts only calls that left the client.
- No capability is "paused" per se — the envelope is workspace-wide
  and the same envelope gates every capability. Once older calls age
  out of the 30-day window, the next call through succeeds again.
- An operator can raise the cap (or lower it) at any time via the
  host CLI; the cached aggregate and the next pre-flight check pick up
  the new value within 60 s.

Refusals are logged at `INFO` with `event = "llm.budget_exceeded"`,
the workspace id, the capability, and the projected overshoot.
Refusals do not hit `audit_log` — they are operational telemetry, not
state changes.

### Pricing table

Per-model USD cost per 1k input and output tokens, kept in a YAML
file (`app/config/llm_pricing.yml`) baked into the image. Loaded at
process start; hot-reloadable via `crewday admin budget reload-pricing`.
An unknown `model_id` in the pricing table falls back to
`(input_per_1k, output_per_1k) = (0.0, 0.0)` **and** logs a
`WARNING` every call. A free-tier model (`:free` suffix on
OpenRouter) is priced at zero — the meter still records the call for
telemetry but the cost contribution is zero.

### Visible surfaces

- **Manager settings panel.** A single tile: "Agent usage — N%" over
  a slim progress bar, subtitled "Rolling 30 days". The tile turns
  red and reads "Paused" when `N >= 100`. No dollars, no tokens, no
  cap value, no reset date.
  - Endpoint: `GET /api/v1/workspace/usage` →
    `{ "percent": 32, "paused": false, "window_label": "Rolling 30 days" }`.
  - `percent` is floored at 0 and capped at 100 for display; internal
    maths keeps the precise ratio for the at-cap decision.
  - Accessible to every user whose grant role passes the existing
    `settings.view` action (owners and managers by default; §05).
- **LLM settings page** (`/settings/llm`). Dollar amounts, token
  counts, and per-capability spend **remain visible here** — this is
  the operator-visibility surface and its audience is the workspace
  owner who hooked up the OpenRouter key in the first place. The
  workspace cap itself is shown read-only, with a "Contact your host
  to change this" note when the current user cannot reach the host
  CLI. No in-app edit control.
- **Worker PWA.** No usage surface. Workers see only the at-cap
  banner inside the chat tab on refusal.

### Demo mode overrides

On the demo deployment (§24), three knobs flip:

- `cap_usd_30d` defaults to **$0.10** for every freshly-seeded
  workspace; overridable per scenario in the fixture.
- The capability whitelist narrows to `chat.manager`, `chat.employee`,
  `chat.compact`, `chat.detect_language`, `chat.translate`, and
  `tasks.nl_intake`. Every other capability is short-circuited to a
  `demo_disabled` error shape (§24 "Disabled integrations") that does
  not consume budget.
- Default model for every running capability is the free-tier
  OpenRouter variant (`google/gemma-3-27b-it:free` in v1). The cap
  stays on as a belt-and-suspenders guard against provider pricing
  changes or a mis-routed call.

A second line of defence, layered on top of the per-workspace budget,
exists only on the demo deployment: a **global daily cap** across
every demo workspace in the container, governed by
`CREWDAY_DEMO_GLOBAL_DAILY_USD_CAP` (default `$5`). Exceeding the
global cap pauses every chat capability on the deployment until UTC
midnight and emits `demo.global_cap_exceeded` to the structured log.
See §16 for deployment wiring and §24 for the demo-side UX.

## Cost tracking — extended

Every LLM call still writes to `llm_call` with the provider's reported
`usage.total_tokens` and the pricing-table cost estimate. The
background worker aggregates daily totals; the owner/manager
`/settings/llm` page still shows rolling 30-day LLM spend per
capability and per model (dollars visible here — see "Visible
surfaces" above). The per-capability `budget_json` caps remain a
useful throttle on a single noisy capability; they run **before** the
workspace envelope check so the capability-level error is preserved
for observability.

## Failure modes

- Provider 5xx: one retry after jitter, then fail the caller;
  capability degrades gracefully (autofill blank, digest skipped,
  etc.).
- Rate-limited: honor `Retry-After`; callers see 503 with a
  `Retry-After` header.
- Content refused / unsafe: return an empty structured output; log
  `finish_reason`; caller surfaces a neutral fallback.
- Per-capability `budget_json` daily cap exceeded: 429 with
  explanation; that capability pauses until UTC midnight.
- **Workspace usage budget exceeded** (see "Workspace usage budget"):
  the client refuses the call before it leaves with the structured
  `budget_exceeded` error shape; the agent surfaces a neutral "Agents
  paused" banner. Older calls age out of the rolling 30-day window
  and capacity returns automatically.
- **Demo capability disabled** (see §24): the call is short-circuited
  client-side to a structured `demo_disabled` error with the
  operation key. The agent is expected to explain the limitation in
  its reply.

## Out of scope (v1)

- Fine-tuning or prompt-caching beyond what OpenRouter offers
  natively.
- Retrieval-augmented generation across the whole DB (we pick
  relevant rows per capability).
- Autonomous long-running agent loops hosted in-process. Agents run
  elsewhere and call the API.
