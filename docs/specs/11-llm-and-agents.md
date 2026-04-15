# 11 — LLM integration and agents

Per the user's direction: **Google Gemma 4 31B IT via OpenRouter** is
the default model, with a per-capability assignment table so other
models can substitute for specific jobs. All in-app agentic features
(natural-language task intake, daily digest, anomaly detection,
receipt OCR, staff chat assistant, agent audit trail, action approval)
share the same plumbing.

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
household default is used.

| capability key             | description                                                             |
|----------------------------|-------------------------------------------------------------------------|
| `tasks.nl_intake`          | Parse a free-text description into a task / template / schedule draft   |
| `tasks.assist`             | Staff chat assistant: "what's next?", explain an instruction, etc.      |
| `digest.manager`           | Morning manager digest composition                                      |
| `digest.employee`          | Morning employee digest composition                                     |
| `anomaly.detect`           | Compare recent completions to schedule and flag anomalies               |
| `expenses.autofill`        | OCR + structure a receipt image                                         |
| `instructions.draft`       | Suggest an instruction from a conversation with the manager             |
| `issue.triage`             | Classify severity/category of an employee-reported issue                |
| `stay.summarize`           | Summarize a stay (for guest welcome blurb drafting)                     |
| `voice.transcribe`         | Turn a voice note into text (for chat assistant / issue reports)        |

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

A manager can edit this in the UI or via
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

The user may override any capability to a different model (e.g.
Claude Haiku for digests, a cheaper Qwen for intake) without code
changes.

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
Retention: 90 days by default, configurable per household (§02).

## Agent audit trail

Every write performed by an agent is already captured in `audit_log`
(§02). Additionally, for agents specifically:

- `audit_log.via = 'api' or 'cli'`, `actor_kind = 'agent'`, `token_id`
  set.
- `audit_log.reason` carries an agent-supplied `X-Agent-Reason` header
  (free text, up to 500 chars).
- `audit_log.correlation_id` propagated from `X-Correlation-Id` if
  present, else generated server-side and returned via
  `X-Correlation-Id-Echo`.

The manager's **Agent Activity** view filters `audit_log` by
`actor_kind = 'agent'` with facets on token, action, and time range,
and a line chart of call volume per token.

## Agent action approval

High-impact actions require a manager to click "Approve" before they
commit, regardless of token scope.

### Which actions

The canonical list, configurable per household:

- Any `*.delete` that would affect more than **10 rows**.
- Employee archive (`employees.archive`).
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
- `employee.set_default_pay_destination`
- `employee.set_default_reimbursement_destination`
- `expense_claim.set_destination_override` (agent path; the manager
  selecting a destination in the approval UI is itself the approval)

### Never-agent endpoints

A separate, stricter class of HTTP endpoints is **not approvable** —
they are refused for agent tokens unconditionally and return
`403 forbidden` with `WWW-Authenticate: error="agent_not_permitted"`.
The approval middleware does **not** write an `agent_action` row
for these, because doing so would itself be the leak: the
middleware persists `resolved_payload_json` and (on execution)
`result_json`, and for these endpoints the response contains
decrypted secret material that must never land in a persisted row.

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
boundary than never-agent — there is literally no network path to
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
2. Middleware detects an approvable action; instead of executing, it
   writes an `agent_action` row with the fully-resolved request
   payload (including idempotency key).
3. Returns `202 Accepted` with
   `{ "approval_id": "appr_…", "status": "pending", "expires_at": "…" }`.
4. Managers are notified (email + webhook `approval.pending`).
5. Manager reviews in `/approvals` and approves or rejects.
6. On approval, the original handler is invoked with the recorded
   payload; result is stored. Agent polls `GET /api/v1/approvals/
   {id}` (or receives `approval.decided` webhook) and proceeds.
7. If `expires_at` passes without decision, status becomes `expired`.

### Model

```
agent_action
├── id
├── approval_id                # human-shown
├── requested_at
├── requested_by_token_id
├── correlation_id
├── action                     # dotted verb
├── resolved_payload_json      # includes resolved URL, method, body
├── idempotency_key
├── state                      # pending | approved | rejected | expired | executed
├── decided_at
├── decided_by_manager_id
├── decision_note_md
├── executed_at
└── result_json
```

### Bypass

A token carrying `admin:*` scope can **not** bypass approval on
default-approvable actions. The only way to disable approval on an
action is for a manager to flip the household-level setting in
`/settings/approvals`.

### TTL

`expires_at` defaults to **7 days** from `requested_at`. Per-action
overrides are allowed (some households may want shorter windows for
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
    "employee_id": "emp_…",
    "template": {…},
    "schedule": { "rrule": "FREQ=WEEKLY;BYDAY=TU", … }
  },
  "assumptions": [
    "Assumed Villa Sud (only match).",
    "Resolved Maria to emp_…",
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
- Sudden drop in an employee's completion rate vs 4-week baseline.
- Inventory consumption deviating > 3σ from rolling mean.

The LLM ranks candidates and writes a one-line explanation per
anomaly. Falsely-flagged items can be "Ignore — stop suggesting this",
persisted in the `anomaly_suppression` table.

### `anomaly_suppression`

| field            | type     | notes                                   |
|------------------|----------|-----------------------------------------|
| id               | ULID PK  |                                         |
| household_id     | ULID FK  |                                         |
| anomaly_kind     | text     | e.g. `task_missed`, `completion_rate_drop`, `consumption_spike` |
| subject_kind     | text     | `task_template` \| `employee` \| `inventory_item` \| ... |
| subject_id       | ULID     |                                         |
| suppressed_until | tstz     | **required** — the UI forces the manager to pick an explicit window when suppressing |
| reason           | text?    | free-form, shown in the digest once the suppression expires |
| suppressed_by    | ULID FK  | manager id                              |
| created_at       | tstz     |                                         |

Scope is `(household_id, anomaly_kind, subject_id)`. Permanent
suppression is not offered: chronic "false positives" usually stop
being false when the underlying pattern shifts, and a forced revisit
keeps the digest honest. A manager who wants a long suppression
enters a correspondingly long `suppressed_until` — the UI defaults
to 90 days, accepts any future date.

## Staff chat assistant

For employees with `chat.assistant` capability on.

- Available as a bottom-nav chat bubble on the PWA.
- Tools exposed to the assistant (subset of the REST API, scoped to
  the current employee):
    - `get_tasks_today()`, `mark_task_done(task_id)`,
      `report_issue(area, description)`, `get_instruction(id)`,
      `start_shift()`, `end_shift()`, `get_inventory_low()`.
- Voice input uses `voice.transcribe` capability; disabled by default.
- Never fabricates tasks: the assistant cannot create arbitrary rows,
  only invoke the exposed tools.

## Cost tracking

Every LLM call writes to `llm_call` with the provider's reported
`usage.total_tokens` and an estimated USD cost from a small pricing
table kept in config. The worker aggregates daily totals and the
manager dashboard shows rolling 30-day LLM spend, per capability and
per model. Exceeding a configured daily cap disables the capability
for the rest of the day (soft-fail: humans still work; agents see a
clear error).

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
