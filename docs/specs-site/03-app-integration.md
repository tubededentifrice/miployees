# 03 — App integration

Two narrow contracts live at the boundary between site and app.
Everything else is isolated.

1. **Feedback RPCs** — `site/api/` → app over HTTP. Three
   endpoints, one per agent-pipeline stage (moderate, embed,
   cluster). All share the same auth, versioning, and error
   shape.
2. **`CREWDAY_FEEDBACK_URL`** — the app renders a "Give feedback"
   link pointing at the site, or renders nothing at all.

Neither contract gives the site any knowledge of real app user
identities — only opaque pseudonymous hashes (`user_hash` and
`workspace_hash`, derived app-side under
`CREWDAY_FEEDBACK_HASH_SALT`; see §02 "Auth flow"). Neither gives
the app any knowledge of submitted feedback bodies beyond what the
site sends in a single request.

## Why not a tighter coupling

- **Shared DB is out.** Two deployables, two DBs. The site is
  optional; the app must boot and run with no site at all.
- **Shared code is out.** `site/api/` does not import from `app/`.
  Cross-package imports break the "independent deploy" promise
  and couple versioning.
- **Webhook-on-event is out for v1.** The app does not push
  anything to the site; site orchestrates pulls. Keeps the app
  stateless w.r.t. the site and avoids a second delivery queue.

## Feedback RPCs

Three endpoints under `/_internal/feedback/`:

| Path | Capability (app §11) | Purpose |
|------|----------------------|---------|
| `POST /_internal/feedback/moderate` | `feedback.moderate` | Decide keep/reject + (on keep) reformulate + embed |
| `POST /_internal/feedback/embed` | `feedback.embed` | Embed one or more texts — used post-hoc for new cluster summaries, operator-triggered re-embeds, and the fallback path when `moderate` returned no vector |
| `POST /_internal/feedback/cluster` | `feedback.cluster` | Assign a reformulated submission to one of a top-K candidate list, or propose a new cluster |

All three share the same auth, the same shared errors, the same
versioning. The `/_internal/` prefix carries the same
`x-agent-forbidden: true` annotation as the rest of the app's
internal routes (app §11, app §12).

### Common headers

```
Host: app.crew.day
Content-Type: application/json; charset=utf-8
Authorization: Bearer <SITE_APP_RPC_TOKEN>
X-Feedback-RPC-Version: 1
X-Feedback-Request-Id: <ULID chosen by the site>
```

- `X-Feedback-RPC-Version: 1` on every call; the endpoint
  accepts exactly the set of versions it advertises via
  `GET /_internal/feedback/manifest` (§"Versioning" below).
- `X-Feedback-Request-Id` is echoed into the app's deployment-
  audit row so cross-stack grepping is trivial.

### Auth

- `SITE_APP_RPC_TOKEN` is a shared static bearer token, 32 random
  bytes base64. Minted by the SaaS operator, provisioned to both
  sides (site's `SITE_APP_RPC_TOKEN`, app's `APP_SITE_RPC_TOKEN`)
  via the deployment secrets manager.
- Rotation is a coordinated deploy — same pattern as §15 root-key
  rotation. Old token is accepted alongside new for a 24 h grace
  window; after that, old token is removed.
- On top of the token, the app enforces a source-IP allowlist
  (`CREWDAY_FEEDBACK_RPC_ALLOW_CIDR`). Default empty → closed; the
  operator sets it to the site container's egress range.
- No mTLS in v1. Adding it is additive — a follow-up.

### Endpoint 1 — `/moderate`

One submission per call. Synchronous on submit; the batch worker
also re-drives `pending` rows through this endpoint.

Request:

```json
{
  "version": 1,
  "submission": {
    "ref": "sub_01JK...",
    "body": "i wish the agent knew which rooms need deep cleaning weekly",
    "category": "idea"
  },
  "policy": {
    "embed": true
  }
}
```

- `body` is **post-redaction** (site §02 "PII posture"). The app
  re-applies its own redaction layer (§11) defensively and aborts
  with `422 redaction_failed` if any well-known PII pattern
  survives. No echo of offending text.
- `category` is the site's enum; the app uses it as a weak
  prompt-shaping hint. Not used for auth or policy.
- No `source` field. Every submission reaching this endpoint is
  authenticated app feedback by construction (§02); the site never
  sends or stores a source discriminator.
- `policy.embed`: `true` means return an embedding in the same
  call (routes to `feedback.embed` internally). `false` means the
  site will call `/embed` separately — useful when the operator
  is reprocessing moderation only.

Response (keep):

```json
{
  "version": 1,
  "moderate_model": "google/gemma-4-31b-it",
  "embed_model": "local/bge-small-en-v1.5",
  "embed_dim": 384,
  "llm_cost_usd": 0.00021,
  "result": {
    "ref": "sub_01JK...",
    "decision": "keep",
    "reformulated_title": "Let the agent set per-room cleaning cadence",
    "reformulated_body": "The submitter wants to tell the agent that some rooms (e.g. bedrooms) need a deep clean weekly, while others need it less often, so the auto-scheduled tasks respect per-room frequency.",
    "detected_language": "en",
    "reasoning": "Clear feature request for per-room cleaning cadence; constructive tone; English.",
    "embedding": [0.0213, -0.0184, ...]
  }
}
```

Response (reject):

```json
{
  "version": 1,
  "moderate_model": "google/gemma-4-31b-it",
  "embed_model": null,
  "embed_dim": null,
  "llm_cost_usd": 0.00008,
  "result": {
    "ref": "sub_01JK...",
    "decision": "reject",
    "moderation_reason": "gibberish",
    "reasoning": "Fewer than 5 real words across any supported language.",
    "reformulated_title": null,
    "reformulated_body": null,
    "detected_language": null,
    "embedding": null
  }
}
```

Notes:

- `decision` is exactly one of `keep` or `reject`. No "maybe".
- `moderation_reason` is set only on `reject`; one of `abuse`,
  `gibberish`, `nsfw`, `off_topic` (matches the §02 enum exactly).
- `reasoning` is a short one-line explanation, ≤ 200 chars.
  Surfaced to the operator via `site-admin submissions show` and
  `site-admin submissions rejected-list` — do not feed it back
  to the submitter.
- `reformulated_title` ≤ 120 chars; `reformulated_body` ≤ 500
  chars. The app enforces both caps on its side.
- `embedding` is present only when `policy.embed=true` **and**
  `decision=keep`. Array of `embed_dim` `float32` numbers, unit-
  normalised (L2). `embed_dim` matches the assigned embedding
  model; a site that expects 384 and receives 1536 MUST reject
  the response and stop the pipeline until the mismatch is
  resolved.

Body cap: 32 KiB request, 256 KiB response (embedding blobs are
JSON-encoded). Above cap → `413`.

### Endpoint 2 — `/embed`

Pure embedding service. No LLM call in this path; the app talks
directly to the configured embedding model (local or hosted; see
app §11).

Request:

```json
{
  "version": 1,
  "texts": [
    "Let the agent set per-room cleaning cadence"
  ]
}
```

Response:

```json
{
  "version": 1,
  "embed_model": "local/bge-small-en-v1.5",
  "embed_dim": 384,
  "llm_cost_usd": 0.0,
  "embeddings": [
    [0.0213, -0.0184, ...]
  ]
}
```

- `texts` is a non-empty array of strings, each ≤ 4 KiB, total
  size ≤ 256 KiB. The response `embeddings` array has the same
  length and the same ordering. Site gets a 400 if lengths
  disagree.
- `llm_cost_usd` is `0.0` for locally-run models; the metering
  still uses the call against the `feedback.embed` deployment
  budget as a safety net if a hosted model ever replaces the
  local one.
- Used by the site for:
  - Embedding a freshly-created cluster summary after
    `/cluster` returns `new_cluster` (the site embeds `summary +
    "\n" + description`).
  - Operator-triggered re-embed of the whole corpus after a
    model swap (`site-admin reembed`).
  - Fallback when `/moderate` returned `decision=keep` but
    `embedding=null` (e.g. because `policy.embed` was false).

### Endpoint 3 — `/cluster`

Assigns one submission to an existing cluster from a candidate
list, or proposes a new one. Batch size of 1 for the sync-on-
submit path; up to 50 for the scheduled batch re-cluster pass.

Request (synchronous, batch of 1):

```json
{
  "version": 1,
  "candidates": [
    {
      "id": "clu_01JK...",
      "summary": "Let the agent assign tasks based on room type.",
      "description": "Short optional 2-3-sentence elaboration."
    }
  ],
  "submissions": [
    {
      "ref": "sub_01JK...",
      "reformulated_title": "Let the agent set per-room cleaning cadence",
      "reformulated_body": "The submitter wants..."
    }
  ],
  "policy": {
    "new_cluster_max": 1,
    "force_existing_only": false,
    "return_new_summary_embedding": true,
    "min_confidence_new": 0.65,
    "min_confidence_existing": 0.5
  }
}
```

Request (scheduled batch):

- Same shape, but `submissions` may be up to 50 items per call,
  each requesting its own assignment.
- `candidates` is the **union** of the per-submission top-K
  candidates the site retrieved locally; the app may use any
  candidate from the array for any submission. Cap `candidates`
  at 200 per call.

Request (batch merge pass):

- A **second** request shape used only by the scheduled merge
  worker:

```json
{
  "version": 1,
  "check_merges": [
    {"src_id": "clu_A", "dst_id": "clu_B",
     "src_summary": "...", "dst_summary": "..."}
  ],
  "policy": { "min_confidence_merge": 0.85 }
}
```

- `submissions` is not present. `candidates` is not present.
  Presence of `check_merges` puts the call into merge-check
  mode.

Response (assign mode):

```json
{
  "version": 1,
  "cluster_model": "google/gemma-4-31b-it",
  "llm_cost_usd": 0.00038,
  "assignments": [
    {
      "ref": "sub_01JK...",
      "cluster_id": "clu_01JK...",
      "new_summary": null,
      "new_description": null,
      "new_summary_embedding": null,
      "confidence": 0.82,
      "reasoning": "Strong semantic match with candidate 1."
    },
    {
      "ref": "sub_01JK...",
      "cluster_id": "new_cluster",
      "new_summary": "Support deep-cleaning cadence per room.",
      "new_description": "Users want per-room cleaning frequency overrides.",
      "new_summary_embedding": [0.032, -0.011, ...],
      "confidence": 0.74,
      "reasoning": "No candidate captures the per-room cadence angle."
    }
  ]
}
```

Response (merge mode):

```json
{
  "version": 1,
  "cluster_model": "google/gemma-4-31b-it",
  "llm_cost_usd": 0.00012,
  "merges": [
    {"src_id": "clu_A", "dst_id": "clu_B", "confidence": 0.91,
     "reasoning": "Both summaries describe per-room cleaning cadence."}
  ]
}
```

Notes:

- `cluster_id` in assign mode is either one of the
  `candidates[].id` values the site passed in, or the literal
  `"new_cluster"`. Any other value → site rejects the response
  as malformed and retries with `force_existing_only=true`.
- `new_summary_embedding` is present iff
  `policy.return_new_summary_embedding=true` and `cluster_id="new_cluster"`.
  Saves a follow-up `/embed` call.
- `confidence ∈ [0,1]`. The site honours
  `policy.min_confidence_*` thresholds — assignments below
  threshold land as `pending` for the next batch.
- `merges` in merge mode includes only pairs that cross
  `policy.min_confidence_merge`. An empty array is a valid
  response.

### Shared errors

| Status | Code | Meaning |
|--------|------|---------|
| `401` | `unauthorized` | Bearer token missing / invalid / rotated out. |
| `403` | `source_blocked` | Request from an IP outside the allowlist. |
| `404` | `not_enabled` | The capability backing this endpoint (`feedback.moderate`, `feedback.embed`, or `feedback.cluster`) is off on this deployment. |
| `409` | `budget_exhausted` | The capability's deployment-scope budget cap is hit; site should back off until next UTC midnight. |
| `413` | `payload_too_large` | Request body exceeded cap. |
| `422` | `redaction_failed` | Post-redaction body still matched a PII pattern (`/moderate` only). |
| `422` | `dim_mismatch` | `/embed` asked to produce a dimension different from what the model emits. |
| `429` | `rate_limited` | Too many calls; carries `Retry-After`. |
| `503` | `llm_unavailable` | Upstream provider down or returned non-JSON; site retries in the next batch window. |

Errors carry `{"error": "<code>", "docs_ref": "docs/specs-site/03-app-integration.md#shared-errors"}` only — no echo of the request, no stack trace.

## Deployment-scope capabilities `feedback.*`

All three RPCs are **deployment-scope** on the app side — they
have no workspace context and meter against a per-deployment
budget rather than the usual per-workspace envelope. App §11 now
carries three new capability rows:

| Capability | Default model | Default monthly cap (USD) | Env toggle |
|------------|---------------|---------------------------|------------|
| `feedback.moderate` | `google/gemma-4-31b-it` | `$10` | `CREWDAY_FEEDBACK_MODERATE_ENABLED` |
| `feedback.embed` | `local/bge-small-en-v1.5` (ONNX, CPU-local) | `$5` | `CREWDAY_FEEDBACK_EMBED_ENABLED` |
| `feedback.cluster` | `google/gemma-4-31b-it` | `$20` | `CREWDAY_FEEDBACK_CLUSTER_ENABLED` |

Notes that apply to all three:

- Each env toggle defaults to `0`. Self-host deployments leave
  them off; the RPCs return `404 not_enabled`. The managed SaaS
  at `crew.day` sets all three to `1`.
- Model assignment goes through the usual `llm.assignments.set
  <capability> <provider_model>` command; the inheritance /
  fallback-chain mechanics are unchanged.
- The redaction layer (§11) runs on every inbound body regardless
  of endpoint, before any model call.
- Audit: every RPC call lands a row in the deployment-audit
  stream (app §15) with fields `rpc`, `capability`, `batch_size`,
  `llm_cost_usd`, `model`, `request_id`, and the client's
  resolved IP.

### Why a new `embeddings` capability tag

`feedback.embed` requires an embedding model, not a chat model.
App §11's `llm_model.capabilities` array gains a new tag —
`embeddings` — signalling that the model exposes an embedding
endpoint. Assigning `feedback.embed` to a model without this tag
returns `422 assignment_missing_capability`.

The v1 seed is **local**: the app ships with
`BAAI/bge-small-en-v1.5` embedded as an ONNX bundle (via
`fastembed`) running in-process. 384-dim, ~30 MB on disk, a few
ms per embed on CPU, zero marginal cost. No external API key
needed. Hosted alternatives (Voyage, Cohere, OpenAI) can be
added as additional `llm_provider` + `llm_provider_model` rows
without schema change.

### Why `feedback.moderate` and `feedback.cluster` are distinct

Could share a model; must not share a capability. Reasons:

- Independent budgets. A spam flood should exhaust the
  moderation budget first, which is cheaper to re-fill, without
  taking clustering down.
- Independent model choices. A future operator may want a tiny
  fast model for moderation (say a 3B classifier) and a larger
  one for clustering.
- Independent audit rows. Operator can track rejection rate and
  new-cluster rate separately.
- Independent prompt templates in the app's prompt library
  (app §11 "Prompt library").

## `CREWDAY_FEEDBACK_URL`

The app-side link that points the existing user at the site. The
site enforces that submits and votes come from authenticated app
users (§02); this subsection spells out the handshake that makes
that possible.

### Configuration

Three deployment-scope env vars, all set together or all left
unset. Partial configuration → app refuses to boot.

| Var | Purpose | Typical value |
|-----|---------|---------------|
| `CREWDAY_FEEDBACK_URL` | Target of the redirect. Site origin + `/suggest`. | `https://crew.day/suggest` |
| `CREWDAY_FEEDBACK_SIGN_KEY` | 32-byte base64 HMAC key. Same value as `SITE_FEEDBACK_SIGN_KEY` on the site. | `<deploy secret>` |
| `CREWDAY_FEEDBACK_HASH_SALT` | 32-byte base64 salt used to derive `user_hash` and `workspace_hash`. App-only; never leaves the app. | `<deploy secret>` |

Boot validator rules (unchanged from the earlier v1 draft plus
the new keys):

- `CREWDAY_FEEDBACK_URL` must parse as `https://…` with a non-
  empty host.
- Host of `CREWDAY_FEEDBACK_URL` must not equal `CREWDAY_PUBLIC_URL`'s
  host (guards against self-referential misconfiguration).
- Path component must end in `/suggest`.
- `CREWDAY_FEEDBACK_SIGN_KEY` and `CREWDAY_FEEDBACK_HASH_SALT`
  must be 32-byte base64-decoded values with ≥ 128 bits of
  randomness.
- Setting any of the three without the others → boot fails with
  a clear pointer to this spec.

Default for all three is unset. Self-host deployments stay unset;
the "Give feedback" link is hidden and the `/feedback-redirect`
endpoint below returns `404`.

### "Give feedback" menu entry

- **Any of the three env vars unset.** No link rendered. No menu
  entry, no footer link, no help-drawer item. Zero runtime
  evidence of these specs on a self-host box.
- **All three set.** The app renders a single "Give feedback"
  entry in the global overflow menu (rendered by the
  `PageHeader` component introduced in commit `403dfeb`). Label
  is i18n'd (`feedback.menuItem`); href is
  `https://app.crew.day/feedback-redirect` (the app's own
  origin). Carries `target="_blank" rel="noopener noreferrer"`.
- The raw `CREWDAY_FEEDBACK_URL` is **never rendered in HTML**.
  The redirect endpoint is the only way to reach the site — that
  way the token mint is always fresh.

### `GET /feedback-redirect`

A new, authenticated-session-only route on the app. It exists to
mint a fresh signed token per click and send the user on to the
site.

```
GET /feedback-redirect
Host: app.crew.day
Cookie: __Host-crewday_session=...
```

Behaviour:

1. Requires a logged-in session (app §03). No session → `302`
   to `/login?return=/feedback-redirect`.
2. Rate-limit: 20 redirects per user per hour. Above → `429`.
3. Computes:
   - `user_hash = HMAC-SHA256(user_ulid, CREWDAY_FEEDBACK_HASH_SALT)[:16]` as hex.
   - `workspace_hash = HMAC-SHA256(workspace_ulid, CREWDAY_FEEDBACK_HASH_SALT)[:16]` as hex.
4. Builds the payload:
   ```json
   {"v":1,
    "uh":"<user_hash>",
    "wh":"<workspace_hash>",
    "exp":<now_unix + 300>,
    "nonce":"<fresh ULID>"}
   ```
5. Signs: `token = base64url(payload_json || "." || HMAC-SHA256(payload_json, CREWDAY_FEEDBACK_SIGN_KEY))`.
6. Responds `302 Found` with `Location: {CREWDAY_FEEDBACK_URL}?t={token}`. Cache-Control `no-store`.
7. Writes an audit row in the deployment-audit stream (§15):
   `feedback.redirect.minted` with the user id, workspace id,
   nonce, and expiry.

Notes:

- The endpoint does not render HTML. On error conditions it
  emits a minimal 4xx with plain text; the app's normal login
  wall handles the unauthenticated case.
- The endpoint is **agent-forbidden** (`x-agent-forbidden: true`)
  — an agent holding a delegated token cannot mint a feedback
  token for its user. Keeps "feedback submitted by an agent on
  my behalf" out of scope entirely.
- `/feedback-redirect` is also rate-limited deployment-wide to
  10 000 per day — a cheap rail against a compromised account
  scripting the mint.

### Site-side verification

The site verifies:

- `exp > now`, else `410 token_expired`.
- HMAC matches, computed with `SITE_FEEDBACK_SIGN_KEY`, else
  `401 token_invalid`.
- `v == 1`, else `400 token_version`.
- `nonce` not present in `used_token_nonce`, else
  `410 token_already_used`.

During key rotation the site accepts both `SITE_FEEDBACK_SIGN_KEY`
and `SITE_FEEDBACK_SIGN_KEY_PREVIOUS` (base64; either may be empty
outside the grace window): on verify, try the current key first and
fall back to the previous. After the 24 h grace window the operator
unsets `SITE_FEEDBACK_SIGN_KEY_PREVIOUS` and the site verifies under
the new key only. Same dual-accept pattern as `SITE_APP_RPC_TOKEN`.

On success it inserts the nonce, sets the `__Host-suggest_session`
cookie per §02, and `302`s to `/suggest` with the `t=` query
stripped.

### Why a signed token (and not just a cookie the app sets)

- App and site are **different origins**: `app.crew.day` can't
  set a cookie readable by `crew.day`. Shared cookies would
  require a parent-domain cookie on `.crew.day`, which leaks
  into every subdomain including the demo.
- A time-bounded, single-use signed token is the standard
  magic-link shape: small, observable, rotatable, and fails
  closed.
- Alternative: OIDC with the app as IdP. Overkill for a
  suggestion-box cookie; adds a whole authz surface and a second
  set of client credentials to manage.

## Versioning

- `X-Feedback-RPC-Version` on every request; `version` on every
  response. Both MUST match and default to `1` in v1.
- **All three endpoints carry the same version.** A breaking
  change to any one bumps the shared version so the site always
  knows what to expect from the trio.
- Breaking changes bump the major version. The app rejects an
  unknown version with `400 unsupported_version`.
- Additive, backwards-compatible changes (new optional fields,
  new error codes) do not bump the version; the app advertises
  its supported versions via `GET /_internal/feedback/manifest`
  (same auth, returns `{ "versions": [1], "endpoints":
  ["moderate","embed","cluster"], "embed_dim": 384,
  "embed_model": "local/bge-small-en-v1.5" }`). The site hits
  the manifest on boot and refuses to start if the dim advertised
  differs from its own migration target.

## Observability

- Site logs every RPC call with a `X-Feedback-Request-Id`
  (ULID generated client-side). App echoes it back as a response
  header and into its own audit row. Correlating a slow submission
  trace (moderate → embed → cluster) across the two stacks is
  one grep on the request id.
- Site-side dashboards track per-run: `batch_size`,
  `llm_cost_usd`, per-capability model + latency, error counters
  broken out by code. Cost anomalies page the operator; routine
  `budget_exhausted` does not.
- App-side keeps each `feedback.*` capability's monthly spend
  visible in the `/admin` shell (app §14 "Admin shell"), same
  surface where other deployment capabilities' spend is shown.
- Reject-rate (`feedback.moderate` rejects ÷ total) surfaces on
  the same admin dashboard as a cheap health signal. A sudden
  spike usually means the moderation prompt drifted.

## Cross-references

- App §11 — capability registry, budget, redaction, audit base.
- App §12 — `/_internal/` convention and `x-agent-forbidden`.
- App §15 — token rotation, envelope posture, workspace vs
  deployment audit scope.
- App §16 — deployment secret handling the RPC token inherits.
- §02 — how the site uses the assignments.
- §04 — how the site enforces the `SITE_APP_RPC_TOKEN`
  provisioning and its secret posture.
