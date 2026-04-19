# 11 — LLM integration and agents

Per the user's direction: **Google Gemma 4 31B IT via OpenRouter**
(`google/gemma-4-31b-it`) is the default model, with a per-capability
assignment table so other models can substitute for specific jobs.
All in-app agentic features (natural-language task intake, daily
digest, anomaly detection, receipt OCR, staff chat assistant, agent
audit trail, action approval, embedded owner/manager and worker chat
agents) share the same plumbing.

**LLM configuration lives on the deployment, not the workspace.**
Provider URLs, API keys, capability → model assignments, pricing
table and deployment-wide spend visibility are deployment-scope
concerns owned by the `/admin` surface (§14 "Admin shell"). Every
workspace shares the same model assignments. The per-workspace
rolling 30-day **budget cap** and **usage meter** (§ "Workspace
usage budget" below) remain workspace-scoped — the envelope is
about how much a single workspace is allowed to spend, not about
which model it uses.

## The agent-first invariant

crew.day is built around a hard rule: **every human UI verb exists
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

## Agent authority boundary

The agent-first invariant stops at three lines. Every route in §12
falls into exactly one of the columns below; the approval pipeline,
the interactive-session-only list, and the host-CLI-only fence are
the three concrete enforcement mechanisms behind them.

| (a) Available to delegated tokens | (b) Gated by approval always | (c) Forbidden to delegated tokens entirely |
|-----------------------------------|-------------------------------|---------------------------------------------|
| Read queries across every resource the delegating user can already read. | Payroll run / issue / pay (`payroll.issue`, `payroll.pay`, §09). | `crewday admin *` — host-CLI-only verbs (§13, "Host-CLI-only administrative commands"). |
| Draft and create writes (tasks, expenses, issues, stays, schedules, messaging) subject to §11 approval and §12 idempotency. | Payout-detail reads (interactive-session-only — see § "Interactive-session-only endpoints" and §09). | `settings.signup_enabled` toggle — flipping self-serve provisioning is deployment-scope. |
| Edits to the delegating user's own preferences, drafts, and self-service rows (leave, availability overrides, personal task lists). | Expense approval above the workspace's configured threshold (`expenses.approve`, §09). | Root-key / envelope-key rotation (`crewday admin rotate-root-key`, §15). |
| `--dry-run` / `--explain` for any endpoint (§13), regardless of whether the underlying verb is in column (b) or (c). | Work-order accept-quote and vendor-invoice approve/mark-paid (§22). | Direct DB operations of any kind — there is no HTTP or CLI surface agents can invoke. |
| Voice transcription for the agent's own chat turn (`voice.*`), when the corresponding capability is on. | Workspace setting changes that move money routing or quotas (default-pay-destination, engagement-kind boundary crossings, §05, §09, §22). | Backup restore (`crewday admin restore`, §16). |
| | Permission-group membership changes (`permission_group.*`) and role-grant edits (§05). | Workspace archive / unarchive and hard-delete purges (`crewday admin workspace archive`, `crewday admin purge`, §02, §15). |
| | Token mint, rotate, and scope grants (`auth.tokens.*`, §03). | `POST /payslips/{id}/payout_manifest` and every other endpoint tagged interactive-session-only — delegated tokens receive `403 session_only_endpoint` regardless of scope (§12). |
| | Bulk destructives above the §11 row threshold (`*.delete` > 10 rows, bulk schedule changes > 50 future tasks). | Deployment-wide settings (`/admin/*` verbs guarded by `deployment.*` grants) — admins reach these through the `/admin` shell agent (§11 "Admin-side agent"), not from a workspace-scoped delegated token. |

The openapi.json annotations are the single source of truth: column
(b) routes carry `x-agent-confirm` (§ "Action confirmation
annotation") **or** a workspace-policy always-gated entry;
column (c) routes carry `x-agent-forbidden: true` (delegated-token
requests reject at the auth middleware before reaching the handler)
or `x-interactive-only: true` (the interactive-session gate in § "Interactive-session-only endpoints").
A CI lint (§17 "CLI parity") fails the build when a new mutating
route is added without either `x-agent-confirm`, `x-agent-forbidden`,
or `x-interactive-only` — there is no "silent" category. See §12 for
the annotation shape and §17 for the enforcement gate.

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

### Admin-side agent

On the `/admin` shell (§14), the same shared `.desk__agent`
component mounts with `role="admin"`. Its characteristics:

- **Principal.** Authority comes from the caller's passkey
  session. The agent uses a delegated token (§03) minted off
  that session; permissions resolve from whichever `role_grants`
  the user holds — both the `(scope_kind='deployment',
  grant_role='admin')` row and any workspace grants they happen
  to have. There is no separate "admin-only token kind".
- **Tool surface.** The union of two CLI descriptors:
  `_surface_admin.json` (deployment verbs) and `_surface.json`
  (workspace verbs, with the global `--workspace` flag resolved
  to whichever workspace the admin is currently discussing).
  This is the literal "admin CLI in addition to the normal CLI"
  the operator asked for. An admin with no workspace grants sees
  only the admin verbs; an admin who also owns a workspace sees
  both, and the agent picks the right one based on the page
  context below.
- **Page context.** Every message from the `/admin` chat carries
  an `X-Agent-Page` header (§12) describing the current admin
  route (e.g. `route=/admin/usage; params=ws=bernard`). The
  resolver injects the decoded context as a labelled system
  section — format mirrors the §11 "Agent preferences" stack:

  ```
  ## Current admin page
  Route: /admin/usage
  Params: ws=bernard
  Entity: workspace Bernard (id=ws_01H…)
  Neighbouring routes: /admin/llm, /admin/workspaces, /admin/settings
  ```

  With this, the admin can say "restart that capability" or
  "trust this workspace" without naming it — the agent resolves
  from context. The header is stored verbatim on the chat
  message and echoed on downstream audit rows as
  `audit_log.ui_page`.
- **Endpoint.** `POST /admin/api/v1/agent/message`,
  `GET /admin/api/v1/agent/log`,
  `GET /admin/api/v1/agent/actions`. Approvals use the same
  `/admin/api/v1/agent/action/{id}/{approve,deny}` shape as the
  workspace-side manager agent, scoped to the admin caller via
  `for_user_id`.
- **Approval pipeline.** Same as the manager-side agent: the
  §11 workspace policy layer and the user's per-user
  `agent_approval_mode` both apply. Deployment verbs the policy
  layer always gates in v1: `deployment.workspaces.archive`,
  `deployment.settings.edit` (root-protected keys),
  `deployment.budget.edit` when the new cap is ≥ 2× the old.
- **Capability key.** `chat.admin`, seeded with
  `google/gemma-4-31b-it`. Distinct from `chat.manager` so an
  operator can run a smaller model for admin chatter without
  weakening the workspace chat.

Default model: `google/gemma-4-31b-it`. Voice is capability-
gated `voice.admin`; default off.

### Agent turn lifecycle

An **agent turn** is the span from a user message landing on an agent
endpoint (`POST /api/v1/agent/{employee,manager}/message`,
`POST /admin/api/v1/agent/message`, or
`POST /api/v1/tasks/{tid}/chat/message`) until the agent produces the
next observable outcome for that thread — an appended message, a
pending approval card, or an error. Every turn is bracketed by two SSE
events so every connected tab and device of the delegating user can
render a "working on it" state without polling:

- `agent.turn.started` — published as the server accepts the user
  message. Payload: `{ scope, task_id?, started_at }`. `scope` is
  one of `employee | manager | admin | task`; `task_id` is required
  iff `scope=task` (mirrors `agent.message.appended`).
- `agent.turn.finished` — published exactly once per matching
  `started`. Payload adds `{ finished_at, outcome }` where `outcome`
  is one of `replied` (an `agent.message.appended` was emitted for
  the same scope), `action` (an `agent.action.pending` was emitted
  — the turn surfaced a gated action instead of a reply), `error`
  (the agent raised), or `timeout` (the server cap fired). The
  server is responsible for pairing; a `started` without a
  `finished` is a bug.

Both events route the existing `/events` (or `/admin/events`) stream
and are scoped to the delegating user the same way
`agent.message.appended` is. Web clients use them to render an
in-log typing indicator — see §14 "Agent turn indicator".

Compaction (below) is a **system operation, not a turn**; it does
not emit turn events. Voice transcription (`voice.*`) runs before a
turn and is surfaced by the mic affordance on the composer, also
not by turn events. Off-app channels (§23) deliver messages but do
not expose turn events — §23 explicitly excludes typing indicators
from the first off-app release.

### Conversation compaction

Both agents accumulate long chat histories. Token budget grows with
thread length; "attention drag" on older, resolved topics degrades
answer quality. Every thread is therefore subject to **compaction**,
shaped by three rules the runtime enforces.

**One live summary, ever.** A thread carries **at most one** live
summary row at a time. A compaction pass emits a single
`chat_message` row with `kind = 'summary'` that *replaces* every
prior summary plus the newly-eligible span of originals. The live
context window therefore contains exactly one summary row, never a
stack of summaries-of-summaries. The superseded summary is not
deleted — its `compacted_into_id` is pointed at the new summary,
preserving the chain for audit and recall.

**Persistence is span-based.** The summary row carries two ULID
columns, `summary_range_from_id` and `summary_range_to_id` (§23
`chat_message`), naming the inclusive range of originals it
covers. Re-compaction widens the span; the originals themselves
never move — they remain in `chat_message`, linked forward via
`compacted_into_id` to whichever summary currently covers them.
The agent's `search_chat_archive(q)` tool searches those originals
(including one-offs stripped from the summary prose; see below) so
a later turn that references an older exchange can pull the real
text back into context on demand. ("chat_archive" is the
agent-facing tool name; under the hood it is the set of
`chat_message` rows with `compacted_into_id IS NOT NULL`.)

**Recent-window floor.** Turns inside the **smaller of the last 24
hours or the last 20 turns** are never compacted. A high-volume
thread (200 turns in an hour) keeps 20 recent originals live; a
quiet thread (5 turns over a week) keeps those 5. The floor is
deliberately tight — the goal is to keep recency honest, not to
defend the window against compaction. Workspace-overridable via
`chat.compact.recent_floor_hours` (default 24) and
`chat.compact.recent_floor_turns` (default 20); the resolver
takes the `min` of the two floors.

**Resolved topics are split into durable facts and one-offs.** The
`chat.compact` capability prompt instructs the model to walk the
eligible span (everything older than the recent-window floor) and
separate each resolved topic into one of two buckets:

- **Durable facts** — statements whose usefulness outlives the
  exchange they appeared in. Example: "*user confirmed no heavy
  lifting until July 2026*", "*Villa Sud alarm is rearmed via the
  kitchen keypad*". These are carried forward verbatim (or
  paraphrased) into the one live summary.
- **One-offs** — completed actions whose outcome is already
  recorded on the canonical entity row, so the chat chatter about
  them is redundant. Example: "*asked to swap Thursday, swap was
  granted*" (the `booking` row carries the swap), "*thanks for the
  list*" (no entity at all), "*uploaded task evidence photo*" (the
  `task_evidence` row carries the photo). These are **omitted
  entirely** from the summary prose. The originals stay in
  `chat_message` and remain reachable via `search_chat_archive` if
  a later turn references them.

The split is decided by the compactor LLM on prompt only — there
is no separate mechanical signal required. The shipped
`pt-compact` prompt (§ "Prompt library") instructs the model to
err toward stripping when the referenced entity carries its own
outcome, on the reasoning that the entity row is the source of
truth and the chat text about it is redundant. The
post-generation numeric-claim verifier (see "Daily digest /
anomaly detection") still runs on the surviving prose. A
mis-classification is recoverable because the originals are still
in `chat_message`; the agent pulls them back via
`search_chat_archive` when a follow-up surfaces.

**Long-term memory lives on Agent preferences, not on the
summary.** When the user asks the agent to remember something
durably ("remember that I only do morning shifts", "please save my
preferred name as Jean rather than Jean-Baptiste"), the agent
calls the existing `agent_prefs.edit_self` verb via its
delegated token (§13 `crewday agent-prefs set me`). That write
flows through the per-user approval mode (`bypass | auto |
strict`, § "Agent action approval") exactly like any other
mutation — the user sees an inline confirmation card rather than
a silent edit. The compactor itself **never** edits preferences;
the `User` preference blob is self-writable only, and "the LLM
decided a fact was durable" is not self-authorship. The line is
hard: durable facts that belong in the preference blob reach it
by a deliberate user request, not by a compaction side-effect.

**Cost.** A compaction pass is a single `llm_call` under
capability `chat.compact`, regardless of whether it is the
thread's first pass or a re-compaction that folds the prior
summary plus a new eligible span. The audit row names the range
of originals consumed via `summary_range_from_id` /
`summary_range_to_id` on the produced `chat_message`.

**Triggers.** Compaction fires when either bound is exceeded:

- More than **200 non-recent turns** exist on the thread, OR
- The oldest non-recent turn is more than **30 days old**.

"Non-recent" means outside the recent-window floor above. Both
bounds are workspace-overridable (`chat.compact.trigger_turns`
and `chat.compact.trigger_days`). A thread that sits entirely
inside its recent-window floor never compacts — the floor wins.

## Agent preferences

A seam for free-form, human-authored guidance that shapes how
agents talk back — the crew.day analogue of stacked `CLAUDE.md`
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
flag `receives_agent_preferences: true` on `llm_assignment` (or on
the capability's entry in the catalog) makes this explicit and lets
a workspace toggle the default for a given capability.

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

## Provider / model / provider-model registry

The LLM plumbing is a three-layer registry modelled on the pattern in
[`micasa-dev/fj2`](https://github.com/micasa-dev/fj2)'s `llm_providers`
app, adapted to crew.day's FastAPI + SQLAlchemy stack and semantic-CSS
front-end. All three tables are **deployment-scope**; workspaces never
see these rows directly. Every table is edited from the `/admin/llm`
graph (§ "LLM graph admin") or its CLI equivalents (§13).

### `llm_provider`

```
llm_provider
├── id                     ULID PK
├── name                   text            -- display name; unique
├── provider_type          text            -- openrouter | openai_compatible | fake
├── api_endpoint           text?           -- overrides the type's default URL
├── api_key_envelope_ref   text?           -- pointer into secret_envelope (§15); never the ciphertext
├── default_model          text?           -- fallback model_id when an assignment lists none
├── timeout_s              int             -- default 60
├── requests_per_minute    int             -- default 60; enforced client-side
├── priority               int             -- lower = tried first when a provider pool is probed
├── is_enabled             bool
├── created_at / updated_at
└── updated_by_user_id     ULID?
```

- **Provider types shipped in v1:**
  - `openrouter` — default; endpoint `https://openrouter.ai/api/v1`.
  - `openai_compatible` — generic OpenAI-shaped HTTP. Covers
    self-hosted gateways (Ollama, vLLM, LM Studio) and secondary
    clouds behind OpenAI-shaped APIs. Requires `api_endpoint`.
  - `fake` — in-process canned responses for tests and fixture-based
    few-shot regressions. Never available in production.
- A native Anthropic SDK adapter is **deferred** to a later version; v1
  reaches Claude models (if an operator wants them) through OpenRouter.
- `api_key_envelope_ref` holds an opaque reference (e.g.
  `envelope:llm:openrouter:default`) that the server resolves through
  `secret_envelope` (§15). The raw key is never returned by the API,
  never logged, and never appears in `llm_call.*` payloads.
- The deployment admin rotates keys with
  `PUT /admin/api/v1/llm/providers/{id}/key`, which generates a new
  envelope ciphertext and updates the ref atomically. This surface is
  `interactive-session-only` (§ "Interactive-session-only endpoints").

### `llm_model`

Provider-agnostic metadata about a model. The same model (e.g.
`google/gemma-3-27b-it`) may be offered by multiple providers.

```
llm_model
├── id                     ULID PK
├── canonical_name         text            -- unique; e.g. google/gemma-3-27b-it
├── display_name           text
├── vendor                 text            -- google | anthropic | openai | meta | mistral | qwen | other
├── capabilities           jsonb           -- list[str]; see "Model capability tags" below
├── context_window         int?
├── max_output_tokens      int?
├── is_active              bool
├── price_source           text            -- '' | 'openrouter' | 'manual'; see "Price sync"
├── price_source_model_id  text?           -- override the id used to look up pricing
├── notes                  text?
├── created_at / updated_at
└── updated_by_user_id     ULID?
```

### Model capability tags

Every `llm_model` row carries a `capabilities` array. crew.day's v1 set,
chosen for the product's actual surface:

| tag               | meaning                                                                        |
|-------------------|--------------------------------------------------------------------------------|
| `chat`            | Standard text chat / completion                                                |
| `vision`          | Accepts image inputs (used by `expenses.autofill`, receipt OCR)                |
| `audio_input`     | Accepts audio inputs directly (future voice capabilities; see `voice.transcribe`) |
| `reasoning`       | Extended-thinking / reasoning models (o-series, GLM-4.6+, Qwen3-Thinking…)     |
| `function_calling`| Native tool-call protocol                                                      |
| `json_mode`       | Guaranteed JSON output via `response_format`                                   |
| `streaming`       | Supports incremental token streaming                                           |
| `embeddings`      | Emits dense vector embeddings for an input text. Required by `feedback.embed`. |

`image_generation` is intentionally **not** in the v1 set — it has no
shipping consumer in crew.day. It joins the list the day a consumer
lands.

Every crew.day capability in the catalog below carries a
`required_capabilities` list. Saving an `llm_assignment` whose
`provider_model` resolves to a model missing one of the required tags
returns `422 assignment_missing_capability` with the concrete diff.
The `/admin/llm` graph renders the offending edge in red until the
assignment is fixed. This is the server-side guard against the
"assigned a text-only model to receipt OCR" foot-gun.

### `llm_provider_model`

The join between provider and model. Pricing and per-combo API tweaks
live here, because the same canonical model can be priced and tuned
differently across providers.

```
llm_provider_model
├── id                         ULID PK
├── provider_id                ULID FK llm_provider
├── model_id                   ULID FK llm_model
├── api_model_id               text            -- what the provider expects on the wire
│                                              -- e.g. 'anthropic/claude-3-5-sonnet' on OpenRouter,
│                                              -- 'claude-3-5-sonnet-20241022' on a native adapter
├── input_cost_per_million     numeric(10,4)
├── output_cost_per_million    numeric(10,4)
├── fixed_cost_per_call_usd    numeric(10,4)?  -- reserved for future providers that bill per-call
├── max_tokens_override        int?
├── temperature_override       float?
├── supports_system_prompt     bool            -- some reasoning models reject system prompts
├── supports_temperature       bool            -- o-series models forbid temperature
├── reasoning_effort           text?           -- '' | 'low' | 'medium' | 'high'
├── extra_api_params           jsonb           -- catch-all for rare/new fields
├── price_source_override      text?           -- '' | 'none' | 'openrouter' — per-row override of the model's price_source
├── price_source_model_id_override text?
├── price_last_synced_at       tstz?
├── is_enabled                 bool
├── created_at / updated_at
└── UNIQUE(provider_id, model_id)
```

- `api_model_id` decouples the canonical model name from what the wire
  expects (OpenRouter prefixes with a vendor, native SDKs don't).
- `supports_temperature = false` makes the client strip the param
  before the call. `supports_system_prompt = false` folds the system
  prompt into the first user turn. These flags exist because they
  matter in practice on o-series / reasoning-first models.

## Client abstraction

```
class LLMClient(Protocol):
    async def chat(
        self,
        *,
        capability: Capability,         -- the assignment resolver picks the chain
        messages: list[Message],
        images: list[ImageRef] = (),
        audio: list[AudioRef] = (),
        tools: list[ToolDef] | None = None,
        response_format: ResponseFormat | None = None,
        max_output_tokens: int | None = None,
        correlation_id: str,
        workspace_id: WorkspaceId | None = None,   -- for the 30-day envelope check
        budget: Budget | None = None,
    ) -> LLMResult: ...
```

The caller passes a `capability` key, not a `model`. The client
resolves the assignment chain for that capability (walking
`llm_capability_inheritance` when needed), runs the workspace-
envelope pre-flight (§ "Workspace usage budget"), and attempts each
assignment in priority order until one succeeds or the chain is
exhausted. Every attempt writes one `llm_call` row so the
per-attempt cost, latency, and `finish_reason` are auditable even
when a rung fails.

`LLMResult` carries `text | tool_calls | structured`, `usage` (prompt
+ completion token counts, dollar estimate), `model_used` (the
`api_model_id` of the rung that succeeded), `assignment_id`,
`fallback_attempts` (0 when the primary worked), and a
`finish_reason`.

## Capability catalog

Each feature names a **capability** key. The assignment table maps a
capability → a priority-ordered chain of `llm_provider_model` rows (see
"Model assignment" below). Every capability declares
`required_capabilities` — the set of `llm_model.capabilities` tags any
assigned model must carry. Attempting to assign a model that fails the
check returns `422 assignment_missing_capability`.

| capability key         | description                                                                 | required_capabilities        |
|------------------------|-----------------------------------------------------------------------------|------------------------------|
| `tasks.nl_intake`      | Parse a free-text description into a task / template / schedule draft       | `chat`, `json_mode`          |
| `tasks.assist`         | Staff chat assistant: "what's next?", explain an instruction, etc.          | `chat`                       |
| `digest.manager`       | Morning owner/manager digest composition                                    | `chat`                       |
| `digest.employee`      | Morning worker digest composition                                           | `chat`                       |
| `anomaly.detect`       | Compare recent completions to schedule and flag anomalies                   | `chat`, `json_mode`          |
| `expenses.autofill`    | OCR + structure a receipt image                                             | `vision`, `json_mode`        |
| `instructions.draft`   | Suggest an instruction from a conversation with the owner/manager           | `chat`                       |
| `issue.triage`         | Classify severity/category of a user-reported issue                         | `chat`, `json_mode`          |
| `stay.summarize`       | Summarize a stay (for guest welcome blurb drafting)                         | `chat`                       |
| `voice.transcribe`     | Turn a voice note into text (for chat assistant / issue reports)            | `audio_input`                |
| `chat.manager`         | Owner/manager-side embedded chat agent (§14 right sidebar)                  | `chat`, `function_calling`   |
| `chat.employee`        | Worker-side embedded chat agent (§14 Chat tab)                              | `chat`, `function_calling`   |
| `chat.admin`           | Deployment-admin embedded chat agent (§14 `/admin` right sidebar)           | `chat`, `function_calling`   |
| `chat.compact`         | Summarise resolved topics in a chat thread (see "Conversation compaction")  | `chat`                       |
| `chat.detect_language` | Detect message language for auto-translation (§10, §18)                     | `chat`, `json_mode`          |
| `chat.translate`       | Translate a message into the workspace default language (§10, §18)          | `chat`                       |
| `documents.ocr`        | Vision fallback for image-bearing documents when local OCR yields no text   | `vision`                     |
| `feedback.moderate`    | **Deployment-scope.** Moderate + reformulate one marketing-site suggestion — called from `site/` over `/_internal/feedback/moderate`. Emits a keep/reject verdict plus (on keep) `reformulated_title`, `reformulated_body`, and `detected_language`. May emit an embedding in-line when `policy.embed=true`. | `chat`, `json_mode`          |
| `feedback.embed`       | **Deployment-scope.** Compute dense embeddings for one or more texts — called from `site/` over `/_internal/feedback/embed`. Used by the suggestion-box pipeline for submission embeddings, cluster summary embeddings, and operator re-embeds. | `embeddings`                 |
| `feedback.cluster`     | **Deployment-scope.** Classify a reformulated marketing-site submission against a site-provided top-K candidate list, or propose a new cluster — called from `site/` over `/_internal/feedback/cluster`. | `chat`, `json_mode`          |

The `required_capabilities` column lives in code — capabilities are a
closed enum declared by the application, not workspace-configurable.
Adding a new capability is a code change that writes both the row in
this table and a seed assignment + prompt-template default.

### Deployment-scope capabilities

Most capabilities in the table above are **workspace-scope**: every
call is attributed to a workspace and meters against that
workspace's rolling 30-day budget (§ "Workspace usage budget").
Three capabilities are **deployment-scope** instead — the three
that drive the marketing site's suggestion box (`crew.day/suggest`).
They are called by `site/api/`, not by anything inside a workspace,
so there is no authenticated user and no `workspace_id` to attribute
the call to. Each therefore meters against a **per-deployment
budget** and its calls land in the deployment audit stream (§15)
rather than the workspace audit log.

| Capability | Default monthly cap | Env toggle (default `0`) |
|------------|---------------------|--------------------------|
| `feedback.moderate` | `CREWDAY_FEEDBACK_MODERATE_MONTHLY_USD_CAP` (default `$10`) | `CREWDAY_FEEDBACK_MODERATE_ENABLED` |
| `feedback.embed`    | `CREWDAY_FEEDBACK_EMBED_MONTHLY_USD_CAP` (default `$5`)  | `CREWDAY_FEEDBACK_EMBED_ENABLED` |
| `feedback.cluster`  | `CREWDAY_FEEDBACK_CLUSTER_MONTHLY_USD_CAP` (default `$20`) | `CREWDAY_FEEDBACK_CLUSTER_ENABLED` |

- Each capability is **off by default**. Turning any of them on
  is a SaaS-operator action; self-host deployments leave them
  off and the corresponding `/_internal/feedback/*` route returns
  `404 not_enabled`.
- Separate budgets on purpose: a spam flood exhausts the
  moderation budget first (smallest cap, cheapest calls) without
  taking clustering down.
- `feedback.embed` default assignment: a **local** model bundled
  with the app image (`BAAI/bge-small-en-v1.5` via `fastembed`,
  ONNX, CPU-native, ~30 MB on disk, 384-dim output, unit-
  normalised). Runs in-process; no external API key, no egress,
  `llm_cost_usd = 0`. Adding hosted embedding providers (Voyage,
  Cohere, OpenAI, Google) is a pure-data change — a new
  `llm_provider` + `llm_provider_model` row plus an
  `llm_assignment` override.
- `feedback.moderate` and `feedback.cluster` default to
  `google/gemma-4-31b-it` — same default chain as other
  classify-small-text capabilities.
- Everything else about these capabilities — redaction layer,
  prompt templating, fallback chain, capability inheritance,
  audit — is identical to the workspace-scope capabilities.
  Future deployment-scope capabilities reuse this pattern.

The full RPC contracts, shared-secret auth, versioning, and
per-endpoint payload shapes live in the site specs — see
`docs/specs-site/03-app-integration.md`. Three additional
deployment-scope env vars wire the app up to the site —
`CREWDAY_FEEDBACK_URL` (the redirect target, must end in
`/suggest`), `CREWDAY_FEEDBACK_SIGN_KEY` (HMAC key used to mint
the magic-link token at `GET /feedback-redirect`), and
`CREWDAY_FEEDBACK_HASH_SALT` (HMAC salt used to derive the
opaque `user_hash` / `workspace_hash` the site keys writes off).
All three default unset; partial configuration → boot fails.
Documented end-to-end in `docs/specs-site/03-app-integration.md`
under "`CREWDAY_FEEDBACK_URL` → Configuration".

## Model assignment

An assignment binds a capability to one `llm_provider_model` at a given
priority. A capability may carry **many** assignments, forming a
priority-ordered **fallback chain** — the client walks the chain on
retryable failures (upstream 5xx, 429, timeout, provider content
refusal, transport error). This is the "proper failback between models"
behaviour; fj2 ships the same shape under the name `LLMAssignment`.

```
llm_assignment
├── id                          ULID PK
├── capability                  text            -- key from the catalog above
├── priority                    int             -- lower = tried first; 0 = primary
├── provider_model_id           ULID FK llm_provider_model ON DELETE PROTECT
├── max_tokens                  int?            -- overrides provider_model + model defaults
├── temperature                 float?
├── system_prompt_override      text?           -- rare one-off; prefer llm_prompt_template
├── extra_api_params            jsonb           -- merged last, wins over provider_model params
├── required_capabilities       jsonb           -- copied from the catalog; recomputed on save
├── is_enabled                  bool
├── last_used_at                tstz?
├── created_at / updated_at
├── updated_by_user_id          ULID?
└── UNIQUE(capability, priority)
```

- The `UNIQUE(capability, priority)` constraint replaces the prior
  `UNIQUE(capability)` rule — a capability can now have
  `(priority=0, primary)`, `(priority=1, fallback)`, etc. Reordering is
  a `PATCH /admin/api/v1/llm/assignments/reorder` bulk operation (the
  CLI exposes `llm assignment reorder`).
- **Retryable errors** that advance the chain: HTTP 5xx from the
  provider, HTTP 429 (rate-limit), client-side timeout, transport
  error, and provider-reported content refusal when `finish_reason` is
  `safety` or equivalent. Explicit `budget_exceeded` from our own
  envelope (§ "Workspace usage budget") does **not** advance — the
  whole workspace is paused, no other assignment will help.
- A chain exhausted with no success surfaces the **last** error to the
  caller, with `X-LLM-Fallback-Attempts` echoing the number of models
  tried. Capability callers degrade per § "Failure modes".
- Per-call token caps move to `extra_api_params`; the existing
  per-capability daily dollar caps are enforced on the `llm_call`
  aggregate. The workspace envelope (§ "Workspace usage budget")
  still runs **before** the chain is entered — once the envelope
  blocks a workspace, no attempt leaves the client.
- Capabilities without assignments fall back through **capability
  inheritance** (below) before the call is considered unassigned. An
  unassigned capability fails closed with
  `503 capability_unassigned` and a `CRITICAL` audit row.

### Capability inheritance

Modelled on fj2's `LLMUseCaseInheritance`:

```
llm_capability_inheritance
├── capability         text PK         -- child
├── inherits_from      text            -- parent; must also be a key in the catalog
└── created_at
```

The resolver walks children to parents: when `chat.admin` has no
enabled assignments of its own, it falls through to `chat.manager`'s
chain. Inheritance also applies to `llm_prompt_template` (below) — a
child capability without a custom prompt uses the parent's. A cycle is
rejected on save with `422 capability_inheritance_cycle`. Child
assignments **override** the parent when present (not merged).

v1 seeds one inheritance row: `chat.admin → chat.manager`. Others
stay flat so operators can reason about their chain in isolation;
new ties are introduced surgically as sub-capabilities appear.

### Capability defaults (seeds)

At first boot the deployment is seeded with:

| capability              | default `provider_model`                                                    | rationale |
|-------------------------|-----------------------------------------------------------------------------|-----------|
| all chat-kind           | OpenRouter × `google/gemma-3-27b-it` (priority 0)                           | Matches the user's Gemma pick. Multimodal, supports JSON mode. |
| `expenses.autofill`     | OpenRouter × `google/gemma-3-27b-it` (priority 0)                           | Same model; `vision` tag drives OCR. |
| `voice.transcribe`      | **No seed** — capability is disabled until an admin assigns an audio model  | No default audio-input model ships. |
| `documents.ocr`         | **No seed** — capability is disabled until an admin assigns a vision model  | Local extractors handle the common cases; the LLM fallback is opt-in per deployment because every call charges the workspace 30-day budget. See §21 "Document text extraction". |

Deployment admins override any chain from `/admin/llm` without a
redeploy. Overrides are deployment-wide.

## Prompt library

`llm_prompt_template` is one of the **hash-self-seeded tables**
defined in §02 — system prompts move from "files on disk" to a
DB-backed library where code declares the default and the DB
carries the operator's customisation. The shared contract (resolver
algorithm, revision twin, admin operations, retention, deployment
scope) lives in §02 "Hash-self-seeded tables"; this section
specifies the prompt-specific columns, seeds, and admin surface.
The pattern is adapted from fj2's `PromptTemplate`.

```
llm_prompt_template
├── id                  ULID PK
├── capability          text            -- identity key; unique while is_active
├── name                text            -- human-readable, defaults to Title-Cased capability
├── template            text            -- full prompt body, Jinja2-compatible
├── version             int             -- auto-incremented
├── is_active           bool
├── default_hash        text(16)        -- sha256[:16] of the code default at the time of last seed
├── notes               text?
├── created_at / updated_at
└── UNIQUE(capability) WHERE is_active = true

llm_prompt_template_revision
├── id                  ULID PK
├── template_id         ULID FK llm_prompt_template
├── version             int
├── body                text
├── notes               text?
├── created_at
├── created_by_user_id  ULID?
└── UNIQUE(template_id, version)
```

### Self-seeding

Code calls `get_active_prompt(capability, default=...)` once per
process for each capability it uses. The resolver implements the
hash-self-seed algorithm defined in §02 "Hash-self-seeded tables"
(create / match / auto-upgrade / preserve). The operator-facing
guarantee — "edit the prompt in the admin UI, keep your
customisation across deploys" **and** "my unmodified prompts stay
in sync with the code" — is the invariant the primitive exists to
provide. The capability-scoped observability log
`template.customised_code_default_changed` carries `capability`
as its identity key.

### Admin operations

Follows the hash-self-seeded admin contract (§02). Concrete paths
for prompts:

- `GET /admin/api/v1/llm/prompts` — list active rows with
  `{version, default_hash, is_customised}`.
- `GET /admin/api/v1/llm/prompts/{id}/revisions` — revision history.
- `PUT /admin/api/v1/llm/prompts/{id}` — operator edit.
- `POST /admin/api/v1/llm/prompts/{id}/reset-to-default` — re-seed
  from the current code default (see §02 on why `DELETE` is not
  offered).
- Retention: `retention.template_revisions_days` (§02), default 365.

### What does and does not go here

Prompt templates carry only the **composition** prompt — the system
message the model sees. They do **not** duplicate the `x-agent-confirm`
card copy (§ "Action confirmation annotation" lives on the API route),
the "Agent preferences" Markdown blobs (§ "Agent preferences" is a
separate table, stacked at render time), or few-shot fixtures
committed as test data. Prompts share the **same injection order**
with preferences at render time:

```
<system prompt body from llm_prompt_template>
<## Workspace preferences ...>
<## Property preferences ...>
<## Your preferences ...>
<grounding observations / tool descriptors>
```

### Prompting strategy (unchanged)

- **Schema-first outputs** wherever feasible: `response_format = json
  schema` through the OpenAI-compatible shape (`json_mode` capability
  required). Callers validate with Pydantic models and fail loudly on
  drift.
- **Grounding context** is assembled from the database and passed as
  structured tool observations or system-message content, never as
  free text inside user messages.
- **Few-shot** for stable shapes (expense OCR, task intake) committed
  as fixtures; `pytest` regressions run against them.

## Price sync

Pricing is DB-authoritative and syncs from OpenRouter on a schedule —
the `app/config/llm_pricing.yml` file from earlier drafts is retired.

- `llm_provider_model.input_cost_per_million` and `.output_cost_per_million`
  are the only cost numbers the cost-tracker reads.
- A worker job `sync_llm_pricing` runs **weekly** (cron
  `0 3 * * 1` UTC), probes every enabled provider whose type matches a
  known price source (v1: OpenRouter only), and updates the
  per-million prices on any `llm_provider_model` where
  `get_effective_price_source()` resolves to that source and
  `price_source_override != 'none'`. Successful rows bump
  `price_last_synced_at`.
- Admins pin a row by setting `price_source_override = 'none'` — the
  sync skips it, and the admin becomes the price authority for that
  combo. The UI surfaces pinned rows with a small "manual" badge.
- `crewday admin llm sync-pricing` triggers a sync from the host
  shell, with the same plumbing as the scheduled job. It prints
  per-row deltas and exits non-zero on any network error.
- Missing prices (row present, price source returns nothing) log a
  `WARNING` per call and keep the existing value. Unknown model IDs
  at call time fall back to `(0.0, 0.0)` and log a `WARNING` per
  call, as before.

The sync job does **not** mutate the model registry — new models
announced by OpenRouter do not auto-appear. Operators import models
explicitly via `/admin/llm` or `crewday admin llm model create`. This
keeps the model catalogue small and intentional.

## LLM graph admin

The `/admin/llm` page presents the registry as a three-column visual
graph (providers → models → assignments), modelled on fj2's
`admin/llm_graph/` interface. The tabular page from earlier drafts is
retired.

- **Column 1 — Providers.** One card per `llm_provider`; shows type,
  endpoint host, enabled state, API-key status (present / missing /
  rotating), and the count of attached provider-models. "Add provider"
  opens the inline edit drawer.
- **Column 2 — Models.** One card per `llm_model`; shows vendor,
  capability tags as chips, context window, and the count of providers
  offering the model. Cards carry a small modality icon bar
  (text / vision / audio / reasoning).
- **Column 3 — Assignments.** Grouped by capability. Each group is a
  vertical stack of its priority chain, top-to-bottom = highest to
  lowest priority. Drag within a group reorders priority (hits
  `PATCH /admin/api/v1/llm/assignments/reorder`). Drag between groups
  is disallowed (a row belongs to one capability).
- **Hover and selection.**
  - Hover a provider card → every model offered by that provider and
    every assignment that resolves through it highlights; everything
    else dims. Same for hover on a model or an assignment.
  - Click any card → inline edit drawer on the right. Escape or
    clicking the backdrop closes it.
- **Validation feedback.**
  - Capabilities with no enabled assignment chain render a red
    "unassigned" pill at the top of their group.
  - Assignments whose `provider_model` fails the
    `required_capabilities` check render with a red outline and a
    hover tooltip listing the missing tags.
  - Pricing rows flagged `price_source_override = 'none'` show a
    small "manual" chip; stale (`price_last_synced_at > 14 days` for
    an unpinned row) show a "stale" chip.
- **Prompt library** lives as a secondary action in the page header —
  "Prompts" opens a slide-over with one row per capability, its
  version, and whether it's currently customised (different body than
  `default_hash`). Editing a prompt opens the revision history.
- **Keyboard.** Global `/` focuses the filter input (filters every
  column at once). `N` on a focused column opens "Add" for that
  column. `Esc` closes the drawer. `j/k` navigates cards within a
  column.

Gated by `deployment.llm.view` (read) and `deployment.llm.edit`
(write), §05. Workspace owners and managers never see this page — the
settings tile on `/settings` keeps the existing "Agent usage — N%"
summary only (§ "Visible surfaces").

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

Every `llm_call` row stores the **redacted** payload sent and the
response received. Original values are never stored on `llm_call`.

```
llm_call
├── id                       ULID PK
├── correlation_id           ULID                -- ties related calls across a logical operation
├── workspace_id             ULID FK             -- for the rolling 30d envelope; null for deployment calls
├── capability               text
├── assignment_id            ULID FK llm_assignment ON DELETE SET NULL   -- which chain rung served the call
├── provider_model_id        ULID FK llm_provider_model ON DELETE SET NULL
├── prompt_template_id       ULID FK llm_prompt_template ON DELETE SET NULL
├── prompt_version           int                                        -- snapshot at call time
├── fallback_attempts        int                                        -- 0 = primary succeeded
├── model_used               text                                       -- denormalised api_model_id
├── input_tokens             int
├── output_tokens            int
├── latency_ms               int
├── success                  bool
├── finish_reason            text?                                      -- stop | length | safety | tool_call | error
├── error_message            text?
├── cost_usd                 numeric(10,6)
├── redacted_prompt_json     jsonb                                      -- what left the client
├── redacted_response_json   jsonb                                      -- what came back
├── raw_response_json        jsonb?                                     -- un-redacted provider body; see below
├── raw_response_expires_at  tstz?                                      -- short TTL; swept by worker
├── actor_user_id            ULID FK users?
├── agent_token_id           ULID FK api_token?
└── created_at               tstz
```

Immutability: `llm_call` rows never update or delete in normal flow —
inserts only. The only modification is the worker's nightly sweep
that nulls out `raw_response_json` / `raw_response_expires_at` once
the TTL passes.

**`raw_response_json`** is the un-redacted provider body kept for
debugging (inspecting tool-call traces, reasoning tokens, seeds,
finish-reason edge cases). It is written only when the deployment
setting `llm.keep_raw_responses = true` (default: true on dev, false
on prod), the corresponding `raw_response_expires_at` defaults to
`now() + 7 days`, and a worker sweep nulls both columns once the TTL
passes. Accessing it requires `deployment.llm.view`; it never appears
in workspace-scoped API responses. This replaces the Redis-backed
debug log used in fj2 — everything lives in the DB with a TTL sweep,
no extra service.

**Retention:** `llm_call` itself keeps its redacted body for 90 days
by default, configurable per workspace (§02). `raw_response_json` is
separate, shorter-lived, and controlled by the settings above.

### Trust boundary for prompt inputs

Not every string that reaches the model is equal. The redaction layer
above handles PII; this subsection handles **prompt injection** — the
separate concern of untrusted text reaching a privileged context and
coaxing the agent into actions its human never asked for. crew.day's
rule is that prompt inputs divide into two tiers and the two tiers
never merge.

**Author-trusted sources.** May reach the model without an extra
redaction pass beyond standard PII scrubbing, because the only
humans who can write them already hold the authority the agent
borrows:

- Owner / manager-authored "Agent preferences" blobs (§ "Agent
  preferences") — the user writes instructions to their own agent.
- SOP and KB bodies authored under `kb.edit` (§07).
- System prompts and capability prompts (`llm_prompt_template`,
  § "Prompt library").
- Fixed server-side scaffolding (resource descriptors, tool schemas,
  the "Current admin page" block).

**Untrusted sources.** MUST flow through the redaction + scrub
layer and MUST NOT be promoted into any author-trusted scope without
a deliberate re-scrub at the boundary:

- Task comments, task titles, and description fields authored by
  any user (including workers and clients).
- Expense descriptions, line-item notes, and amount-justification
  text.
- Receipt OCR output and any other text extracted from uploaded
  files (§08, §21).
- iCal event names and descriptions pulled from external calendar
  feeds (§04).
- Guest welcome-page inputs (wifi notes, guest-provided fields).
- Chat-adapter message payloads arriving from external channels
  (§23) — WhatsApp/Telegram/email body content before it reaches
  the agent dispatcher.
- Uploaded file contents broadly — PDFs, images, text documents —
  once extracted by OCR or text conversion.

**Boundary rule.** Any server-side code that assembles an
author-trusted blob (preference stack, SOP injection, prompt
template, admin-page context) from data that originated in an
untrusted source MUST re-run the redaction + scrub layer at the
boundary, with a `trust_source='untrusted'` tag so the scrub is not
skipped on a "preferences already redacted" cache hit. Preferences
are **never** computed from task data, expense data, or chat
payloads; that path is forbidden, not merely gated. Review the
seam when adding a new preference source.

See §15 "Personal task visibility" and §07 "KB authoring" for the
downstream consequences — a visibility bug on either side would
turn an untrusted input into an author-trusted one by accident.

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

### Approval decisions travel through the human session, not the agent token

The approval model is the Claude-Code-style inline confirmation: the
user is already authorised to do the thing, the card is there so they
see what their agent is about to do and can say yes or no before it
commits. **A user can absolutely self-approve their own agent's
actions** — they originated the intent and they carry the permission.
What the spec pins is which credential carries the decision, so a
compromised delegated token cannot close its own loop.

- Approval decisions
  (`POST /api/v1/agent/action/{id}/{approve,deny}` and its admin
  sibling) MUST be submitted under a **passkey-authenticated user
  session**. Requests authenticated by any delegated token
  (scoped or delegated under §03) reject with
  `403 approval_requires_session`. The user's own browser clicking
  "Confirm" on the chat card is the expected path; a second agent
  call is not.
- The approving session may belong to the **same user** who
  delegated the initiating token — this is the common single-owner
  case. What matters is the credential type (session vs delegated
  token), not that two different humans are in the loop.
- Personal API tokens (PATs, §03) that a human minted for their own
  CLI use count as session-class for this purpose *only* when they
  carry the `approvals:act` scope, which is off by default and
  must be granted explicitly at mint time. This lets a CLI-driven
  owner approve from a terminal without opening the browser, at
  the cost of one deliberate scope grant that the owner can revoke
  later. Delegated agent tokens can never hold this scope.
- Workspace-policy always-gated actions in `/approvals` follow the
  same rule (decision must come via a session or a PAT with
  `approvals:act`). They do *not* require a second human — a single
  owner can review their agent's payroll run from their own
  `/approvals` desk. The queue is a visibility surface, not a
  two-person control.

Audit-log fields make the decision chain legible after the fact:

- `action.agent_token_id` — token that proposed the action (may be
  `NULL` for actions proposed by a passkey session rather than a
  delegated-token agent).
- `action.initiator_user_id` — the delegating user behind that
  token; denormalised on the `agent_action` row so rotations and
  revocations do not lose the identity.
- `decision.approver_user_id` — the human who clicked
  approve/deny, resolved from the approver's passkey session (or,
  for PAT-submitted approvals, the PAT's subject user).
- `decision.approver_credential_kind` — `session` | `pat`.
  Delegated-token submissions never reach this field; they reject
  at the auth middleware.

The corresponding audit row is `audit.approval.granted` or
`audit.approval.denied`; a rejected submission writes
`audit.approval.credential_rejected` with the offending token id so
attempted closed-loop bypass surfaces in the Agent Activity view
(§ "Agent audit trail") rather than failing silently.

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
- `crewday admin llm sync-pricing` — triggers the OpenRouter pricing
  sync on demand (same plumbing as the weekly worker job in § "Price
  sync"). Prints per-row deltas and exits non-zero on network error.
  Replaces the earlier `budget reload-pricing` verb; the on-disk
  `llm_pricing.yml` file is retired in favour of the DB
  (`llm_provider_model.input_cost_per_million` etc).

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
| `web_admin_sidebar`   | Deployment-admin web client — `.desk__agent` on the `/admin` shell (§14 "Admin shell"). `for_user_id` is the admin caller; pending cards show only in their `/admin` chat, not in any workspace's `/approvals` desk (the deployment has no `/approvals`). |
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

## Agent knowledge tools

Agents never read the operator's filesystem. Two well-defined virtual
file surfaces are wired as in-process tools so the model can pull on
demand instead of carrying everything in every prompt. Both are
exposed only to chat-kind capabilities (`chat.manager`,
`chat.employee`, `chat.admin`); composition capabilities (digests,
OCR, anomaly detection) do not run in a conversation and have no use
for them.

### System docs

Code-shipped Markdown that explains how the agent should behave —
the CLI cheat-sheet, what an `x-agent-confirm` card means, how to
phrase a digest, how to react when the worker is on mobile, when to
hand off to the manager. The crew.day analogue of an `AGENTS.md`
that the agent itself reads.

- Source files live under `app/agent_docs/*.md` in the codebase and
  ship with the deployment. Front-matter declares
  `slug`, `title`, `summary`, `roles: [manager | employee | admin]`,
  and an optional `capabilities: […]` allow-list (defaulting to all
  three chat capabilities).
- The `agent_doc` table (§02) is one of the **hash-self-seeded
  tables** (§02 "Hash-self-seeded tables"); it seeds from these
  files on boot. A `default_hash` mismatch with the current code
  default auto-upgrades unmodified rows and preserves operator
  edits.
- Operators edit per-deployment overrides at `/admin/agent-docs`
  (a slide-over on the `/admin/llm` page, mirroring the prompt
  library) or via `crewday admin agent-docs edit <slug>`.
- Reset-to-default and revision history follow the shared
  hash-self-seeded admin contract (§02). Retention follows
  `retention.template_revisions_days` (§02).

The two tools exposed to the agent:

| tool                       | description                                                                                                  |
|----------------------------|--------------------------------------------------------------------------------------------------------------|
| `list_system_docs()`       | Returns `[{slug, title, summary, updated_at}]` for every active doc whose role tag matches at least one of the delegating user's role grants. |
| `read_system_doc(slug)`    | Returns the full Markdown body. The resolver caches per LLM turn so repeated reads are free.                 |

System docs are **not redacted** — they are operator-trusted. The
editor banner on `/admin/agent-docs` reads "Body is sent to every
chat agent that loads this doc. Do not paste workspace secrets,
customer data, or live API keys."

### Knowledge base (instructions + documents)

The agent's view of the workspace's actual content — the manuals,
warranties, contracts, SOPs, certificates the user can already read
in the UI — is a single combined search + read surface that walks
two existing sources:

- `instruction` revisions (§07) — already injected into the system
  prompt for the in-scope set; the KB tools let the agent pull
  *other* instructions on demand without inflating every turn.
- Extracted `asset_document` text (§21) — newly available in v1
  via the extraction pipeline below.

Two tools, exposed to the same chat-kind capabilities as system docs:

| tool                                                                | description                                                                                                                                                            |
|---------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `search_kb(q, *, property_id?, asset_id?, kind?, limit=10)`         | Top-N ranked hits across instruction revisions and extracted document text, filtered by the delegating user's read access. Each hit is `{kind, id, title, snippet, score, why}`. |
| `read_doc(ref, page=1)`                                             | Returns the full body for an instruction or a paginated extracted-text window for a document. Default page size **4 000 model-tokens**. Returns `{kind, title, body, page, page_count, more_pages, source_ref}`. |

`ref` is `{kind: "instruction" \| "document", id}`. `kind` on the
search filter accepts `instruction \| document` (omit for both) and
`{document_kind: manual \| warranty \| ...}` for the §21 enum.

`why` is a short human-readable provenance string the model can
quote back ("Manual for *Daikin AC* at *Villa Sud*, page 3"). It is
the analogue of the §07 "Linked to this task template" badge.

Both tools call REST endpoints (§12 `GET /kb/search`,
`GET /kb/doc/{ref}`) **through the delegated token**, so the agent
can never see a doc the delegating user cannot see. The server-side
authorisation is the same one that already guards
`GET /documents/{id}` and the instruction read APIs — no new action
key is introduced.

For documents whose `extraction_status` (§02 `file_extraction`,
§21) is not `succeeded`, the row is omitted from `search_kb` and
`read_doc` returns the structured stub:

```json
{
  "kind": "document",
  "id": "doc_…",
  "extraction_status": "extracting" | "failed" | "unsupported" | "empty",
  "hint": "I haven't been able to read this manual yet — extraction is still running. Try again in a minute."
}
```

The agent is expected to surface the hint to the user in plain
language, not retry tightly.

### Why a tool, not a system-prompt dump

Three reasons:

1. **Context stays small.** The combined manual library for a
   workspace with thirty assets can run to 50 000+ tokens; injecting
   it every turn is wasteful and degrades attention on the actual
   question.
2. **Permission boundary is naturally enforced.** Tool calls flow
   through the delegated token (memory: *the agent never elevates*).
   There is no "filtered system prompt" surface to keep in sync
   with role grants as they change.
3. **Audit is honest.** `llm_call.redacted_prompt_json` carries the
   tool result the model actually used; `redacted_response_json`
   carries the tool calls it issued. A read on a document that
   never reached the model leaves no trace beyond the REST hit
   logged on `audit_log` (§ "Read auditing" below).

### Read auditing

Reads through `search_kb` and `read_doc` produce one
`audit_log` row apiece with action `kb.search` /
`kb.doc.read`, the resolved `instruction_id` or
`asset_document_id`, and the same delegated-token attribution as
any other agent action (§ "Agent audit trail"). They are **not**
agent-action approvals — reads never gate. The rows are visible in
the user's own audit trail and on the manager's "Agent activity"
filter so a worried owner can see "the agent looked at this
contract twice this week."

`list_system_docs` and `read_system_doc` do **not** write
`audit_log` rows — they read shipped, operator-edited content, not
workspace state. They are visible only via the LLM-call trace.

### Redaction posture

KB content goes through the standard §11 redaction layer at
injection time — the same scrub already applied to instruction
bodies and free-text fields.

- `email`, `phone_e164`, `full_legal_name` → tokenised; addresses
  truncated to city.
- Hard-drop secret patterns (Wi-Fi passwords, alarm codes,
  IBAN-shaped tokens, API tokens, OAuth bearers) are **stripped
  from extracted text before the snippet leaves the client** and
  replaced with the marker `[redacted secret]`. The same
  hard-drop list as agent preferences (§ "PII posture") is reused
  to keep the policy in one place.
- A document whose extracted text contains a hard-drop secret is
  flagged `file_extraction.has_secret_marker = true` and the
  `/documents/{id}` UI shows a small warning so the operator can
  re-upload a less sensitive version if the secret was accidental.

System docs are operator-shipped and **not** redacted. Agent
preferences (§ "Agent preferences") remain the only PII-pass-through
surface — the carve-out stays narrow and intentional.

### Document text extraction (capability)

A new capability key, intentionally separate from
`expenses.autofill`:

- `documents.ocr` — vision-model fallback used by the extraction
  worker when local libraries (pypdf, pdfminer, python-docx,
  openpyxl, Tesseract) yield no usable text from an image-bearing
  upload. Required model capability: `vision`.

The capability is **disabled by default** (no seed assignment).
Local extractors handle text PDFs, office documents, and image
PDFs with an OCR layer; the LLM fallback is opt-in per deployment
because every call eats workspace budget (§ "Workspace usage
budget"). When unassigned, an image-only upload with no extractable
text records `extraction_status = unsupported` and surfaces the
hint to the agent and to the upload UI.

Pipeline mechanics, storage, and retry behaviour are specified in
§21 "Document text extraction"; the capability key and the budget
linkage live here.

### Cross-references

- `agent_doc` schema and the seeding algorithm: §02 "Shared tables".
- Instruction grounding rules: §07 "LLM use".
- Extraction pipeline, status enum, retry policy, REST surface:
  §21 "Document text extraction".
- KB search index (FTS5 / tsvector ranking weights): §02 "Full-text
  search ranking — knowledge base".
- Demo posture (capability disabled, smaller index): §24.

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
      `get_my_bookings(from?, to?)`, `amend_booking(id, ...)`,
      `decline_booking(id, reason?)`, `propose_booking(...)`,
      `get_inventory_low()`.
- Voice input uses the `voice.enabled` setting plus the
  `voice.transcribe` model assignment; disabled by default.
- Never fabricates tasks: the assistant cannot create arbitrary rows,
  only invoke the exposed tools.

## Cost tracking

Every LLM call writes to `llm_call` with the provider's reported
`usage.total_tokens` and an estimated USD cost computed from the
serving `llm_provider_model` row's per-million prices (§ "Price sync"
keeps them current). The background worker aggregates these rows into
the rolling meter used by the **workspace usage budget** (§ "Workspace
usage budget" below) and into the per-capability daily breakdowns on
the `/admin/llm` page. Per-call `max_tokens` caps live on the
assignment / provider-model / model cascade; the workspace envelope is
enforced before the client picks a chain rung, as an envelope over
every capability.

## Workspace usage budget

One layer above the per-capability daily dollar caps (enforced on the
aggregated `llm_call` totals): a **workspace-wide rolling dollar
budget** that envelopes every LLM capability charged to the workspace.
The per-capability caps stay — they remain useful to throttle a single
noisy capability — but the product-level question "how much is this
workspace spending on agents?" is answered by this envelope, and so is
the "stop me before I overspend" guard.

A further layer above this one — for open self-serve deployments —
aggregates `llm_call.cost_usd` across every workspace sharing a
`signup_ip` (or IPv6 `/64` prefix). That **per-IP aggregate LLM
spend cap** is the deploy-wide guard against a single actor
provisioning many unverified workspaces to multiply the
per-workspace cap; see §15 "Per-IP aggregate LLM spend cap" for
the cap formula, the promotion-on-verification rule, and the
`ip_budget_exceeded` error surface.

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

Raising or lowering a workspace cap has **two equivalent paths**:

1. **Host-CLI.** `crewday admin budget set-cap --workspace <id>
   --cap-usd <value>` remains supported — the operator is always
   the ultimate authority. No HTTP, no auth beyond container
   shell access. Writes `audit.workspace_budget.updated` with
   `via = 'cli'` and `actor_kind = 'system'`.
2. **`/admin`.** Any user who passes `deployment.budget.edit`
   (§05) can adjust a cap from `/admin/usage` or
   `PUT /admin/api/v1/usage/workspaces/{id}/cap`. Writes the
   same row; `via = 'api'` and `actor_kind = 'user'`. Agent
   callers flow through the approval pipeline like any other
   mutating deployment verb.

Workspace owners and managers see their own usage tile on
`/settings` but have no cap-edit control — the commitment-to-
spend stays with the deployment admin. See "Visible surfaces"
below.

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

### Pricing source

Per-model USD cost per 1 M input and output tokens lives on
`llm_provider_model` (§ "Provider / model / provider-model registry")
and is kept current by the weekly `sync_llm_pricing` job (§ "Price
sync"). An admin who pins a row (`price_source_override = 'none'`)
becomes the price authority for that combo. An unknown `api_model_id`
at call time falls back to `(input, output) = (0.0, 0.0)` per-million
**and** logs a `WARNING` every call. A free-tier model (`:free`
suffix on OpenRouter) is priced at zero — the meter still records
the call for telemetry but the cost contribution is zero.

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
- **Admin LLM page** (`/admin/llm`). The three-column graph
  (providers → models → assignments) plus the prompt-library
  slide-over, dollar amounts, token counts, per-capability spend,
  per-workspace spend, provider key status, and the sync-pricing
  trigger all live here (§ "LLM graph admin"). Gated by
  `deployment.llm.view` / `deployment.llm.edit` (§05). This is the
  sole operator-visibility surface for LLM internals in v1; it
  replaces the per-workspace `/settings/llm` page from the earlier
  design.
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
`usage.total_tokens` and the cost estimate computed from the serving
`llm_provider_model`. The background worker aggregates daily totals
into `llm_usage_daily` (one row per `(day, workspace_id, capability,
provider_model_id)`), which powers the per-capability breakdowns on
the `/admin/llm` page and the rolling 30-day meter. The per-capability
daily dollar caps remain a useful throttle on a single noisy
capability; they run **after** the workspace envelope check so the
workspace-level refusal wins when both would fire.

## Failure modes

- Provider 5xx: one retry after jitter, then fail the caller;
  capability degrades gracefully (autofill blank, digest skipped,
  etc.).
- Rate-limited: honor `Retry-After`; callers see 503 with a
  `Retry-After` header.
- Content refused / unsafe: return an empty structured output; log
  `finish_reason`; caller surfaces a neutral fallback.
- Per-capability daily dollar cap exceeded (aggregate over
  `llm_call.cost_usd` for that capability on the current UTC day):
  429 with explanation; that capability pauses until UTC midnight.
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
- Retrieval-augmented generation across **arbitrary entity tables**.
  The grounding surfaces in v1 are: (a) the structured row sets each
  capability already pulls in, (b) the §07 instruction injection,
  (c) the agent knowledge tools (§ "Agent knowledge tools") covering
  instruction revisions and extracted document text, and (d) the
  on-demand system docs. Anything outside these surfaces still
  requires a tool call to a typed CLI/REST verb.
- Vector embeddings. The KB index stays on the existing FTS5 /
  tsvector path (§02 "Full-text search ranking — knowledge base").
  Embeddings join the day FTS recall proves insufficient, with a
  measurable consumer first.
- Autonomous long-running agent loops hosted in-process. Agents run
  elsewhere and call the API.
- Native Anthropic / OpenAI / Z.AI SDK adapters. v1 reaches every
  provider through the OpenAI-compatible shape (including OpenRouter
  and self-hosted gateways). Adapters can land later without
  schema changes — `llm_provider.provider_type` is an open enum.
- `image_generation` and `embedding` capability tags. Neither has
  a shipping consumer; both join the closed capability enum the
  day one does.
- Auto-import of models from OpenRouter's `/models` endpoint. The
  weekly sync touches prices only; the model catalogue is curated by
  hand via `/admin/llm`.
- A Redis dependency. Rolling meter, daily summaries, and
  `raw_response_json` all live in the DB with worker-driven TTL
  sweeps. If a deployment grows past SQLite's comfort, the migration
  is Postgres, not Redis.
