# 23 — Chat gateway

The **chat gateway** is the single seam through which a user's embedded
agent (§11) exchanges messages with that user — no matter which
channel the user reached for. The web sidebar (`.desk__agent`), the
worker PWA Chat tab, and WhatsApp are all **channels**; the agent
runtime, the tool surface, the delegated token, the audit trail, and
the approval pipeline are **shared**. Telegram, push, and future
transports plug in through the same adapter protocol with no changes
to the agent brain.

This spec names the contract the transports must satisfy and the data
model the runtime persists. §11 remains the authority on what the
agent does with a message once it has one; this document is about how
messages get in, get out, and get bound to the right user.

## Principles

1. **One agent per user, many channels.** The agent runtime in §11 is
   one code path per user. Channels carry envelope metadata (sender
   address, interactive affordances, language hints, quiet-hours
   semantics) and nothing semantic. The tool surface and the
   delegated token are identical regardless of the channel the turn
   arrived on.
2. **No implicit auth.** A channel message authenticates as a user
   only through an explicitly linked **binding** row, created by a
   challenge-response ceremony. An inbound message from an
   unrecognised address is logged for rate-limiting and silently
   dropped — the gateway never replies to an unbound sender
   (avoiding amplification and anti-spam exposure).
3. **Agent-first invariant (§11) is preserved.** Every human-facing
   verb reachable in the web agent is reachable over WhatsApp. The
   only differences are presentation (interactive buttons vs web
   approval cards) and constraints specific to the transport
   (Meta's 24-hour session window, text-length caps).
4. **Humans do not talk to humans on off-app channels.** The gateway
   is for user ↔ **agent** conversations. Cross-user messaging stays
   on email (§10) and task threads (§06). WhatsApp is not a DM
   system.
5. **The gateway does not hold business rules.** Scheduling, payroll,
   expenses, approvals all live in their own specs. The gateway only
   transports, binds, translates, and renders.

## Channel catalog

Canonical list. `channel_kind` is the stable slug; new entries ship
with a code migration and an OpenAPI update.

| `channel_kind`        | transport                    | direction   | v1 status      |
|-----------------------|------------------------------|-------------|----------------|
| `web_owner_sidebar`   | FastAPI SSE / `/api/v1/agent/manager/*` | both | shipping (§14) |
| `web_worker_chat`     | FastAPI SSE / `/api/v1/agent/employee/*` | both | shipping (§14) |
| `offapp_whatsapp`     | Meta Cloud API (WhatsApp Business) | both | **v1 new**     |
| `offapp_sms`          | RFC-compliant SMS gateway    | agent→user only | v1 (agent reach-out only) |
| `offapp_telegram`     | Telegram Bot API             | both        | deferred — §19 |

`offapp_sms` remains outbound-only in v1 because SMS has no
interactive-button primitive and the reply parser would have to
disambiguate free-text `YES` across every pending approval. Agent
reach-out over SMS is still covered by §10.

`offapp_telegram` is listed so `ChannelAdapter` (below) and
`chat_channel_binding.channel_kind` accept it from day one; the
adapter implementation is in §19 "Beyond v1".

## Bindings

A **binding** ties a `(channel_kind, address)` pair to exactly one
`users` row. It is the sole primitive the gateway uses to resolve
inbound envelopes to an identity.

### `chat_channel_binding`

| column                | type     | notes                                                                 |
|-----------------------|----------|-----------------------------------------------------------------------|
| id                    | ULID PK  |                                                                       |
| workspace_id          | ULID FK  | the workspace the binding is scoped to (see "Multi-workspace users")  |
| user_id               | ULID FK  | `users.id`                                                            |
| channel_kind          | text     | one of the catalog slugs above                                        |
| address               | text     | normalised transport address — E.164 for `offapp_whatsapp` / `offapp_sms`, `@handle` for `offapp_telegram`; `null` is not allowed |
| address_hash          | text     | deterministic HMAC-SHA256 of `address` with the workspace key; the plaintext `address` is redacted from LLM contexts (§15) but the hash supports O(1) inbound lookup |
| display_label         | text     | user-chosen, e.g. "Personal phone" — shown on `/me`                   |
| state                 | text     | `pending | active | revoked`                                          |
| created_at            | tstz     |                                                                       |
| verified_at           | tstz?    | set on successful challenge; required when `state = 'active'`         |
| revoked_at            | tstz?    | set on user revoke, STOP keyword, or archive of `users` row           |
| revoke_reason         | text?    | `user | stop_keyword | user_archived | admin | provider_error`        |
| last_message_at       | tstz?    | for 24h-session-window tracking (see "Session window")                |
| provider_metadata_json | jsonb?  | opaque per-provider envelope (e.g. Meta `waId`, Telegram `chat_id`)   |

**Uniqueness.** Partial unique index on `(channel_kind, address_hash)
WHERE state != 'revoked'` — one active or pending binding per
address per kind, globally across the deployment. Revoked bindings
retain their rows for audit and can be re-verified through a fresh
ceremony (new `id`, new challenge). Combined with the
`(user_id, channel_kind)` cap below this enforces "one WhatsApp per
user **and** one user per WhatsApp."

**Per-user cap.** Partial unique index on `(user_id, channel_kind)
WHERE state != 'revoked'` — one binding per channel kind per user
(the decision recorded in this session). A user can hold one
WhatsApp, one Telegram, one SMS binding simultaneously.

**Multi-workspace users.** v1 ships single-workspace so every
binding is scoped to that workspace. When true multi-tenancy lands
(§19), the partial-unique on `(channel_kind, address_hash)` is
scoped to `workspace_id` and inbound routing disambiguates by
asking the user to pick a workspace on first contact. The schema
already carries `workspace_id`; no data migration.

### Link ceremony

A user binds an address through a **profile-initiated
challenge-response**:

1. User opens `/me → Chat channels` (§14) and picks "Link WhatsApp"
   (or Telegram). UI collects the address, normalises, validates.
2. `POST /api/v1/chat/channels/link/start` inserts a
   `chat_channel_binding` row with `state = 'pending'` and a
   per-binding `chat_link_challenge` row:

   ```
   chat_link_challenge
   ├── id                ULID PK
   ├── binding_id        ULID FK → chat_channel_binding.id
   ├── code_hash         argon2id hash of a 6-digit code (Crockford base32 for Telegram)
   ├── code_hash_params  argon2id params (upgradeable)
   ├── sent_via          text — `channel` (send code over the channel being linked) | `email` (fallback)
   ├── attempts          int, capped at 5
   ├── expires_at        tstz (15 minutes)
   └── consumed_at       tstz?
   ```

   The code is sent to the target address through the channel
   being linked. For WhatsApp, the outbound uses a **template
   message** (Meta's rule for first-contact outbound). The
   workspace's pre-registered template is named
   `chat_channel_link_code` and takes exactly one positional
   parameter — the code.
3. User replies to the same conversation (channel-side) or enters
   the code in the UI (`POST /api/v1/chat/channels/link/verify`).
   The gateway accepts **either** path:
   - Inbound message that parses as the code for a pending
     binding whose address matches the sender → binding becomes
     `active`, `verified_at = now()`, `consumed_at` set.
   - UI POST with the correct code for the binding id → same
     effect.
4. Five wrong attempts burn the challenge; the binding remains
   `pending` with `verified_at` null until it expires (24h), at
   which point it is auto-revoked with
   `revoke_reason = 'provider_error'`.

**Re-verification.** If `state = 'revoked'`, the user can start a
fresh ceremony from the profile page; a new binding row is inserted
(old row preserved for audit).

**Archived users.** When `users.archived_at` is set, a worker flips
every active binding to `revoked` with
`revoke_reason = 'user_archived'`. The gateway then ignores
subsequent inbound from those addresses.

## Data model for chat

Every channel — web included — lands its traffic in a single pair of
tables, so the compaction, search, and audit logic from §11 are
channel-agnostic.

### `chat_thread`

| column              | type     | notes                                                      |
|---------------------|----------|------------------------------------------------------------|
| id                  | ULID PK  |                                                            |
| workspace_id        | ULID FK  |                                                            |
| user_id             | ULID FK  | the user whose agent runtime this thread belongs to        |
| primary_channel_kind | text    | the channel the thread was opened on (display hint only)   |
| started_at          | tstz     |                                                            |
| last_turn_at        | tstz     |                                                            |
| archived_at         | tstz?    |                                                            |

There is **one live thread per user** in v1. New channels join the
same thread rather than creating a fresh one, so the agent has a
single ongoing conversation regardless of surface. A user who
suddenly replies on WhatsApp after a week of web chat picks up
where they left off.

### `chat_message`

| column              | type     | notes                                                                 |
|---------------------|----------|-----------------------------------------------------------------------|
| id                  | ULID PK  |                                                                       |
| thread_id           | ULID FK  |                                                                       |
| workspace_id        | ULID FK  |                                                                       |
| kind                | text     | `user | agent | system | action`                                      |
| direction           | text     | `inbound | outbound` — inbound is user→agent, outbound is agent→user  |
| channel_kind        | text     | which channel this turn traversed                                     |
| binding_id          | ULID FK? | set for off-app channels; null for web                                 |
| provider_message_id | text?    | the channel-native id (Meta `wamid`, Telegram `message_id`, etc.) — used for replay defeat and delivery correlation |
| body_md             | text?    | post-translation copy in the workspace default language               |
| body_md_original    | text?    | as received; mirrors `task_comment.body_md_original` (§10)            |
| language_original   | text?    | BCP-47 detected language                                              |
| translation_llm_call_id | ULID? | `llm_call.id` for the translation pass (§11)                        |
| agent_action_id     | ULID FK? | for `kind = 'action'` rows, the linked approval row (§11)             |
| file_ids            | ULID[]   | attachments (photos, voice notes, documents) materialised as `file` rows (§02) |
| delivery_state      | text     | outbound only — `queued | sent | delivered | read | failed`; inbound rows are always `delivered` on arrival |
| failure_reason      | text?    | provider-side error, free-form                                        |
| sent_at             | tstz     |                                                                       |
| delivered_at        | tstz?    |                                                                       |
| read_at             | tstz?    |                                                                       |
| compacted_into_id   | ULID FK? | set when a resolved topic is folded into a summary message (§11 compaction) |

Unique index on `(binding_id, provider_message_id)` where both are
non-null — defeats replay of the same provider message id. Inbound
messages whose `provider_message_id` already exists are silently
discarded with a `chat_message.duplicate_inbound` audit line.

The existing §10 `offapp_delivery` table is **retired** in favour of
`chat_message` above; the worker migration drops it. The same rows
(state, retries, provider_message_id) now live on `chat_message`
with `direction = 'outbound'`. This removes the duplication and
matches the unified gateway.

## Routing

### Inbound

1. Provider webhook hits `POST /webhooks/chat/<channel_kind>`
   (authenticated by the provider's webhook signature, verified
   against the per-workspace secret in `secret_envelope` — §15).
2. Adapter parses the envelope to `(channel_kind, address, body,
   files[], provider_message_id, raw)`.
3. Gateway looks up `chat_channel_binding` by
   `(channel_kind, address_hash)` WHERE `state = 'active'`.
   - No active binding → inbound is silently logged under
     `chat.gateway.ignored_unbound` and dropped. The sender
     receives nothing. Optional per-workspace policy:
     auto-reply once per unique address per 24h with
     "This number isn't linked to a miployees account. Visit your
     profile → Chat channels to link it." Default **off** (prevents
     becoming a reply bot for wrong numbers).
   - Active binding but `state = 'pending'` and body parses as a
     link code → see "Link ceremony" step 3.
4. Keyword short-circuit — before dispatching to the agent, check:
   - `STOP` / `ARRET` / `ARRÊT` / `STOPP` → binding flipped to
     `revoked` with `revoke_reason = 'stop_keyword'`; one template
     message acknowledging the opt-out (STOP is a regulatory
     requirement).
   - `HELP` / `AIDE` / `?` → canned help reply naming the workspace,
     how to unlink, and the URL to the web app. No agent call.
   - `PAUSE <duration>` → sets the binding's `quiet_until` field
     (added as part of this spec); the agent will not reach out
     over this binding until the timestamp passes. Inbound from the
     user during the pause resumes the agent immediately.
5. Otherwise, a `chat_message` row with `direction = 'inbound'` is
   inserted, the thread's `last_turn_at` is advanced, and the
   agent runtime is invoked in-process with the turn appended to
   context. The agent's tool calls, approvals (§11), and
   responses produce further `chat_message` rows with
   `direction = 'outbound'`.

### Outbound

The gateway picks the channel for an agent-initiated outbound turn
by this rule:

1. If the current turn is a reply to a user-initiated turn on
   channel `C`, use `C`.
2. Else, if the user has an active binding and
   `preferred_offapp_channel = <that kind>` and the workspace's
   `agent.reachout_offapp` policy allows it, use that binding
   subject to the 24h session window (see below).
3. Else, queue the reply for **next time the user opens the web
   app** — write the `chat_message` with `delivery_state =
   'queued'` and channel `web_*`; the SSE subscription delivers on
   reconnect.

This means an agent working on a task can send reminders over
WhatsApp to a user who prefers WhatsApp, but will not spam them at
2am (see "Quiet hours").

### Session window (WhatsApp)

Meta requires that outside a 24-hour window starting at the user's
most recent inbound message, outbound must be a pre-approved
**template message**. The gateway tracks `last_message_at` on the
binding (updated on every inbound) and on each outbound:

- If `now - last_message_at ≤ 24h` → free-form text is allowed.
- If `> 24h` → only registered templates. The gateway ships two
  registered templates in v1:
  - `chat_channel_link_code` — used during the link ceremony.
  - `chat_agent_nudge` — used for agent reach-out; takes one
    parameter, a short body; expands to "Hi {display_name}, your
    household assistant has an update: {body}. Reply to continue."
- Attempts to send free-form text past the window are **auto-
  wrapped** in `chat_agent_nudge` by the adapter, with the original
  body as the `{body}` parameter. A note is added to the message's
  `delivery_state` audit trail. If the agent needs to send something
  that won't fit the template, it must defer and retry once the
  user speaks.

Telegram and SMS have no equivalent constraint; the rule is
WhatsApp-specific.

### Quiet hours

Unchanged from §10:

- Each user has a quiet-hours window on their profile (default
  21:00–08:00 local).
- Agent-initiated outbound over any off-app channel is **deferred**
  until the window closes; queued turns wait on the worker.
- User-initiated inbound is never blocked — if the user messages
  during quiet hours, the agent replies at once on the same
  channel.
- `PAUSE` keyword (above) extends quiet hours per-binding.

### Rate caps

- Per-user per-binding **inbound** cap: 30 messages per minute
  (workspace-configurable). Messages above the cap are dropped with
  a single throttle-reply per minute.
- Per-user per-day **agent-initiated outbound** cap: 5 messages
  (workspace-configurable, matches §10 today).
- Per-workspace per-day **outbound** cap on the Meta number: 1000
  (Meta's business-initiated conversation caps apply on top).
- Reply-to-inbound messages do not count against the outbound cap.

## Interactive affordances

The gateway exposes a minimal surface that all interactive channels
must support. Adapters map to their native primitives.

| affordance       | WhatsApp mapping                        | Telegram (future) | web                          |
|------------------|-----------------------------------------|-------------------|------------------------------|
| `text`           | text message body                       | text              | bubble                       |
| `buttons`        | interactive reply buttons (≤ 3)         | inline keyboard   | approval card CTAs           |
| `list_choice`    | interactive list (≤ 10)                 | inline keyboard   | select                       |
| `media_image`    | image message                           | photo             | inline `<img>`               |
| `media_document` | document                                | document          | file download                |
| `media_audio`    | voice note                              | voice             | audio element                |

The web channels support the same affordance set so the agent never
has to pick its wording based on surface. Adapters either render
natively or degrade to text (with the affordance payload serialised
as a trailing block) — the runtime never fails a turn over
affordance support.

### Approval cards on WhatsApp

§11's per-user approval mode and workspace approval policy produce
`agent_action` rows with a rendered `card_summary`, `card_fields`,
and `card_risk`. On the gateway:

- The adapter sends an **interactive button** message with the
  title `card_summary`, the body serialised from `card_fields` (up
  to 1024 chars, truncated with "… open the app for full details"
  if longer), and two buttons `Approve` / `Reject`.
- The button reply carries a provider-generated id that maps to
  `approve` or `reject`. The gateway looks up the pending
  `agent_action` by `(for_user_id, state = 'pending')` in the
  binding's workspace and applies the decision through the same
  `/approvals/{id}/{decision}` handlers the desk uses.
- **Replay defeat** — the `provider_message_id` of the reply is
  checked against `chat_message`. A button pressed twice decides
  once. Separate bindings cannot cross-decide (the resolver scopes
  by `(workspace_id, for_user_id)`).
- **High-risk** (`card_risk = 'high'`) actions are **not** resolved
  on WhatsApp in v1. The card includes the two buttons but the
  handler returns a short message: "This one needs a full review —
  please open the app to confirm." The approval still lives in
  `/approvals`. Owner/manager oversight is the anchor on
  money-routing verbs.
- **TTL** — the 7-day `agent_action.expires_at` from §11 applies
  regardless of channel. A user who ignores the WA card for a week
  sees it expire; the agent re-requests if still relevant.

This flips §11 "Inline approval UX" for `offapp_whatsapp` from
deferred (Beyond v1) to v1. `offapp_sms` stays deferred — see
channel catalog.

## Media handling

Inbound media is fetched by the adapter (Meta Cloud returns a media
id; SMS and Telegram return a URL), stored in a `file` row (§02)
tagged with the binding id, and attached to the `chat_message` by
id. The agent sees the file reference exactly as it sees a
web-uploaded photo.

Three primary consumers:

- **Expense claim** (§09) — when the agent's reasoning classifies
  an inbound image as a receipt, it invokes
  `POST /api/v1/expenses/from_image` (new — wraps
  `expenses.autofill` from §11 plus the expense-claim creation) and
  surfaces the result as an approval card under the user's mode.
- **Task evidence** (§06) — when the agent has an open task
  requesting photo evidence, the inbound image can satisfy the
  evidence requirement. The agent confirms the task and uploads
  the photo to `task_evidence` under a delegated token.
- **Issue report** (§10) — when the user describes a problem with
  an attached photo, the agent drafts an `issue` row and confirms
  via an approval card.

Voice notes are transcribed through capability `voice.transcribe`
(§11). Default **off** in v1; flipping it on is per-user
(`voice.employee` / `voice.manager`). Untranscribed voice notes
are stored as files and the agent replies "I can't listen to voice
notes yet — please type or turn on voice transcription in your
profile."

EXIF stripping (§15) applies to every image that lands in a `file`
row, regardless of source.

## Provider abstraction

```
class ChannelAdapter(Protocol):
    channel_kind: str

    def parse_inbound(self, payload: Any) -> InboundEnvelope: ...
    async def send_text(self, binding: Binding, body: str) -> SendResult: ...
    async def send_buttons(self, binding: Binding, title: str,
                           body: str, choices: list[Button]) -> SendResult: ...
    async def send_media(self, binding: Binding, file: FileRef,
                         caption: str | None = None) -> SendResult: ...
    async def send_template(self, binding: Binding, template: str,
                            params: list[str]) -> SendResult: ...
    async def fetch_media(self, provider_media_id: str) -> bytes: ...
    def verify_webhook(self, headers: Headers, raw_body: bytes) -> bool: ...
```

v1 ships one implementation: **Meta Cloud API** for
`offapp_whatsapp`. The SMS adapter remains outbound-only and
implements only `send_text` / `send_template` / `verify_webhook`.
Telegram is specified by the same interface; the adapter is in §19.

Provider credentials (WhatsApp access token, phone-number id,
business-account id, webhook verify-token, template registrations)
live in `secret_envelope` (§15) under
`purpose = 'chat_channel.<kind>'`. The owner/manager configures
them on `/settings → Chat gateway`; tokens are never rendered in
the UI (`display_stub` only). Rotation follows the same rules as
any other envelope secret (§15).

## REST surface (v1)

New routes, all under `/api/v1/chat`:

```
GET    /chat/channels                      list current user's bindings
POST   /chat/channels/link/start           begin a link ceremony for the current user
POST   /chat/channels/link/verify          verify a code (or the inbound side auto-verifies)
POST   /chat/channels/{id}/unlink          user-initiated revocation
GET    /chat/threads/current               current user's live thread (§ one-thread-per-user)
GET    /chat/threads/{id}/messages         paginated messages in a thread
POST   /chat/threads/{id}/messages         send a user message (web channel)

POST   /webhooks/chat/whatsapp             Meta Cloud webhook — adapter-owned
GET    /webhooks/chat/whatsapp             Meta Cloud verify challenge

# Owner/manager administration:
GET    /chat/admin/bindings                all bindings in the workspace (manager scope)
POST   /chat/admin/bindings/{id}/revoke    admin-initiated revocation (user lost phone, etc.)
GET    /chat/admin/provider                provider config display (stubs only)
PUT    /chat/admin/provider                update provider credentials (envelope-stored)
```

All mutating routes honor `Idempotency-Key` (§12) and are CLI-
mapped (§13 `x-cli`). Approval flow follows §11: `*/unlink` and
`*/revoke` carry `x-agent-confirm` annotations.

## Events

Added to §10 webhook catalog:

```
chat_channel_binding.*  created, verified, revoked, link_expired
chat_message.*          received, sent, delivered, failed
chat_thread.*           opened, archived
```

Per-event payloads redact the plaintext address; subscribers
receive the binding id, channel kind, display label, and message
metadata. Full address is available via an authenticated
`GET /chat/admin/bindings/{id}` for manager-session users only.

## Security

Most of the gateway's security posture is delegated:

- **Authentication** — §03 delegated tokens; the gateway minted a
  delegated token for the agent runtime as part of each turn,
  scoped to the binding's user.
- **Authorisation** — the user's `role_grants` (§02). A worker
  cannot do what a worker cannot do, regardless of channel.
- **Audit** — every write is an `audit_log` row with
  `actor_kind = 'user'`, the delegating user, `agent_label =
  'chat-gateway:<channel_kind>'`, and `agent_conversation_ref =
  '<thread_id>:<chat_message.id>'` (§11).
- **Approval** — §11 approval pipeline; inline rendering over
  WhatsApp per this spec.
- **PII** — §15 redaction: `address` is hashed for lookups and
  redacted from LLM prompts; `address_hash` is NOT sent upstream;
  provider credentials live in `secret_envelope`.

Gateway-specific hardening:

- **Webhook signature** verification is enforced on every inbound;
  a missing or bad signature returns 401 before any DB touch.
  Meta's `X-Hub-Signature-256` is HMAC-SHA256 over the raw body
  with the per-workspace webhook secret.
- **Sender spoofing** — Meta's provider envelope includes a
  `wa_id` that is cryptographically bound to the phone number on
  registration; we trust it. For channels without equivalent
  guarantees (future Telegram), the adapter documents the binding
  quality and the link ceremony still requires a one-time code.
- **Lost phone** — user follows the §03 re-enrollment flow
  (re-issue magic link → fresh passkey → session). Bindings are
  **not** automatically revoked on re-enrollment — the phone
  number is still the user's. If the phone itself was stolen, the
  user (or any `users.revoke_grant` peer) hits
  `POST /chat/admin/bindings/{id}/revoke` explicitly. This keeps
  the common case (new device, same SIM) friction-free while
  naming the stolen-phone recovery path.
- **Replay** — `chat_message.provider_message_id` uniqueness
  index defeats double-decide on approvals and double-ingest on
  media.
- **Prompt injection** — an inbound image caption or voice-note
  transcript is untrusted content; §11's tool-call whitelist and
  structured-output schemas already gate what the agent can do
  with it.

## UX / surface (§14)

- `/me → Chat channels` (every user): list current bindings,
  display label, state, last-used timestamp; buttons to link a new
  channel, unlink an existing one, toggle
  `preferred_offapp_channel`, and edit quiet hours.
- `/chat-channels` (owner/manager): workspace-wide view of
  bindings, delivery error rates, per-user
  opt-out status, Meta provider health ("last webhook received at
  …", "template sync status"). Route is owner/manager-only and
  listed in §14's route contract.
- `/settings → Chat gateway`: provider config (Meta credentials,
  verified templates, webhook URL copy button), workspace
  reach-out policy, quiet-hours default, rate caps.
- Inline rendering in the web chat surfaces (§14 sidebar and PWA
  Chat tab) is unchanged — they now consume `chat_message` rows
  by thread and render affordances from the same schema WhatsApp
  receives.

## Out of scope (v1)

- Telegram adapter implementation (seam is ready — §19).
- SMS inline approvals (no interactive primitive — channel
  catalog).
- Cross-user WhatsApp DMs (not the gateway's purpose).
- Real-time typing indicators.
- Inbound calls / video (no channel supports it).
- End-to-end encryption between the user and the agent — Meta's
  transport is E2E to their servers, but the workspace runtime
  must read message bodies to reason about them.
- Multiple active bindings per `(user, channel_kind)` (deferred;
  the schema allows historical revoked rows already).

## Gaps vs. mocks

- A stub `ChatChannelsPage` is present at
  `mocks/web/src/pages/manager/ChatChannelsPage.tsx`; the
  per-user `/me` Chat-channels section is an inline card on
  `MePage.tsx`. The link-ceremony modal is a non-interactive
  placeholder in v1 mocks.
- Provider config UI on `/settings` is a labeled stub.
- The mock `chat_channel_binding` seed carries one active
  WhatsApp binding for the default worker and one pending
  binding for the default owner-manager.
