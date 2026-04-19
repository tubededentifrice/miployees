# 02 — Suggestion box

The agent-clustered feedback surface at `crew.day/suggest`.
Authenticated app users submit ideas from inside the product; the
agent moderates and reformulates each one; the app clusters
submissions into themes; the public board shows the largest and
most recent clusters so the team can see what matters without
reading every message.

**Submit and vote require authentication; viewing the board is
public.** The site is never an anonymous submission surface —
that would put an unauthenticated write path on the marketing
origin, and bring with it a captcha/rate-limit arms race we don't
need. Every write is attributable to a pseudonymous app user
(§ "Auth flow" below).

The site owns the form, the data, and the pipeline cadence. The
app owns the LLM and embedding calls — it moderates, reformulates,
and embeds each submission, then classifies it against a top-K
short-list the site retrieved locally (§03). The site stores
everything.

## Actors

- **App user (submitter).** Signed into their app workspace; clicks
  "Give feedback" in the app overflow menu; lands on `crew.day/suggest`
  with a signed, short-lived token minted by the app. Can submit,
  vote, and read the board. Rate-limited per user (§ "Rate limits").
- **Marketing-site visitor.** No app account. Can **read** the
  public board and cluster detail pages. Cannot submit or vote —
  submit/vote UI shows a clear "Log in to your crew.day workspace
  to submit an idea" CTA with a link to the app's login.
- **Site operator.** Person running `crew.day`. Uses a host-only
  CLI inside `site/api/` (`site-admin clusters hide <id>`,
  `submissions unreject <id>`, etc.). No web UI in v1.
- **Agent pipeline.** The app's three `feedback.*` deployment-scope
  capabilities (§03, app §11) — moderate, embed, cluster.
  Stateless per call.

The site **never learns the user's email, ULID, display name, or
workspace slug**. Submissions and votes are keyed by a
`submitter_user_hash` (HMAC of the user's ULID under a shared
salt) and `submitter_workspace_hash` (same treatment for the
workspace ULID). The site only ever sees hashes — enough to
attribute writes and compute "N people at your workspace also
asked for this" without holding any identifiable data.

## Surfaces

- `/suggest` — split view: the form on the left, the public board
  on the right (stacked on narrow viewports). The form panel
  renders differently per auth state:
  - **Auth cookie present** — full form + vote controls enabled.
  - **No auth cookie** — the form panel shows a compact
    "Log in to your workspace to submit or vote"
    call-to-action with a single button linking to
    `https://app.crew.day/login?return=%2Ffeedback-redirect`.
    The board itself stays fully interactive for reading.
- `/suggest/cluster/<id>` — static page per visible cluster. Shows
  the cluster summary, count, the reformulated titles of the
  five most recent non-hidden submissions, vote widget
  (disabled without auth), "N people at your workspace also
  asked for this" line when the visitor has an auth cookie.
- `/suggest/thanks` — post-submit landing. Confirms receipt and
  shows the cluster the submission joined if stage 3 matched
  synchronously; otherwise says "We group ideas every few
  hours — yours will show up on the board once we've grouped
  it."
- `crew.day/api/suggest/*` — the backend routes the islands call.
  Served by `site/api/`, not by Astro. See §03 and §04 for
  origin / routing. Writes (submit, vote) require the auth
  cookie; board reads are open.

## Auth flow

The only way into the form or vote controls is through the app.
The handshake is a signed, single-use token minted server-side by
the app when the user clicks "Give feedback".

```
     app user clicks "Give feedback"
     ┌─────────────────────────────┐
     │ app.crew.day                │
     │ GET /feedback-redirect      │───┐  mints { user_hash,
     │  (logged-in session only)   │   │    workspace_hash, exp,
     └─────────────────────────────┘   │    nonce }, HMAC-signed
                                       │  under CREWDAY_FEEDBACK_SIGN_KEY
                                       ▼
     302 → https://crew.day/suggest?t=<token>
                                       │
                                       ▼
     ┌─────────────────────────────┐
     │ crew.day                    │   verifies HMAC, exp, nonce-
     │ GET /suggest?t=<token>      │───┐  freshness; sets __Host-
     │                             │   │  suggest_session cookie for
     └─────────────────────────────┘   │  12 h; drops `t` from URL
                                       ▼  via 302 self-redirect.
                                 form + vote unlocked
```

### Token contents and verification

- **Signing key:** `CREWDAY_FEEDBACK_SIGN_KEY` on the app side,
  same value as `SITE_FEEDBACK_SIGN_KEY` on the site — a 32-byte
  shared HMAC key, same rotation pattern as `SITE_APP_RPC_TOKEN`
  (§03 and §04). Rotation is coordinated.
- **Token format:** URL-safe base64 of
  `payload || "." || HMAC-SHA256(payload, key)` where
  `payload` is the JSON blob
  `{"v":1,"uh":"<user_hash>","wh":"<workspace_hash>","exp":<unix_ts>,"nonce":"<ulid>"}`.
  `user_hash` and `workspace_hash` are HMACs themselves — see
  § "Hash derivation" below.
- **Token lifetime:** 5 minutes (`exp`). Short enough that a
  leaked link is nearly dead on arrival; long enough to absorb
  clock skew and slow mobile networks.
- **Single-use:** every `nonce` the site accepts lands in a
  `used_token_nonce` table with `expires_at = exp + 30s`. A
  replay with the same nonce is rejected as
  `410 token_already_used`. The table is pruned by a 15-minute
  cron.
- **Redirect hygiene:** after a successful verify the site
  issues a `302` to `/suggest` **without** the `t=` query — so
  the token never persists in the address bar, in referer
  headers, or in bookmarks. The cookie carries forward; the URL
  does not.

### Session cookie

After verification the site sets:

```
Set-Cookie: __Host-suggest_session=<sid>; Secure; HttpOnly;
            SameSite=Lax; Path=/; Max-Age=43200
```

- `sid` is an `itsdangerous`-signed opaque blob carrying
  `{user_hash, workspace_hash, issued_at, exp}`. The site
  verifies the signature on every request; tampering → treat as
  absent.
- `Max-Age=43200` (12 hours). Long enough to finish the thought
  the visitor came with; short enough that a borrowed device
  doesn't leave an open write channel for weeks.
- `SameSite=Lax` is correct — navigation from app.crew.day to
  crew.day carries the cookie. The site does not do cross-site
  POSTs from anywhere else.
- Cookie is **site-origin only**; the app never reads or writes
  it.
- No `crewday_csrf` double-submit on top: the cookie is
  `__Host-` scoped (path `/`, Secure, HttpOnly) and every site
  write endpoint checks `Sec-Fetch-Site` is `same-origin`.

### Hash derivation

To keep the site pseudonymous:

```
user_hash      = HMAC-SHA256(user_ulid, CREWDAY_FEEDBACK_HASH_SALT)[:16 bytes → hex]
workspace_hash = HMAC-SHA256(workspace_ulid, CREWDAY_FEEDBACK_HASH_SALT)[:16 bytes → hex]
```

- `CREWDAY_FEEDBACK_HASH_SALT` is a 32-byte deployment secret on
  the **app** side (§03); the site never sees it. The app
  computes both hashes before signing the token, so the site
  receives already-hashed values.
- Rotation of the salt is a scorched-earth operation. Three things
  break together because each derives from the salt:
  - **Per-workspace aggregates** ("N people at your workspace asked
    for this") collapse: pre-rotation `workspace_hash` values no
    longer collide with freshly-minted ones.
  - **Vote uniqueness per user** breaks: a user's pre-rotation
    `voter_user_hash` ≠ post-rotation hash, so the UNIQUE
    `(cluster_id, voter_user_hash)` constraint will let the same
    real user vote twice — once under each hash.
  - **Per-user dedupe of prior submissions** stops matching across
    the rotation boundary, since the dedupe key is
    `submitter_user_hash`. A user could re-submit an idea they
    already filed and the duplicate-detection path (§ "Rate limits
    and abuse controls") would not catch it.
  Documented as a scorched-earth operation in §04 secret inventory.
- The site stores hashes verbatim in submission / vote rows;
  there is no second-level hashing site-side.

### Sign-in failure modes

- **No auth cookie and no `t=` param.** Visitor sees the public
  board plus the "Log in" CTA on the form panel. No 401, no
  redirect — the board is still useful.
- **Expired or tampered token.** Rendered as a short
  `/suggest?err=token_invalid` page with a fresh "Log in" link.
  No silent retry.
- **Token for a workspace or user the site has already banned.**
  See § "Abuse controls". Presents as `403 banned`; the
  operator can look up the hash in the CLI if needed, but the
  site does not display identifying detail.

## Submission form

Three fields. Only `body` is required. The visitor must be
authenticated; an unauthenticated caller hitting the submit
endpoint gets `401 unauthenticated`.

| Field | Required | Max | Notes |
|-------|----------|-----|-------|
| `body` | yes | 4 000 chars | Free-form English or any locale the agent can read. Rejects < 10 chars with "Could you give us a bit more detail?" |
| `category` | no | enum | Visitor-selected chip: `idea`, `bug`, `question`, `other`. Default `idea`. Never a drop-down; large touch targets. |
| `notify_email` | no | 254 chars | Optional address for update emails. Stored only if the box "Email me when this is acted on" is ticked. Visitor may prefer a different address from their workspace email — kept as a free field. |

No `form_token`, no captcha, no `source` field. Every submission
is `source='app'` by definition.

## Data model

Site-owned tables in the site's own SQLite / Postgres database. No
foreign keys into any app table — the site is standalone. Vector
columns are backed by **sqlite-vec** (virtual table companion) on
SQLite and **pgvector** on Postgres; the schema below names the
logical column, the dimension, and the distance metric.

```
feedback_submission
├── id                         ULID PK
├── body                       text — verbatim, redacted per "PII posture"; stored but NEVER publicly rendered
├── category                   text — idea | bug | question | other
├── submitted_at               tstz
├── submitter_user_hash        bytea(16) — app-derived HMAC of user ULID; opaque to site
├── submitter_workspace_hash   bytea(16) — app-derived HMAC of workspace ULID; opaque to site
├── notify_email               text NULL — present only if opt-in ticked
├── detected_language          text NULL — two-letter ISO, set by the moderation pass
├── cluster_id                 ulid NULL — set on successful cluster assignment
├── status                     text — pending | accepted | clustered | rejected | hidden | spam
├── moderation_decision        text NULL — keep | reject (set by the agent pipeline)
├── moderation_reason          text NULL — abuse | gibberish | nsfw | off_topic (on reject)
├── reformulated_title         text NULL — ≤120 chars; set on keep; THIS is what the public board shows
├── reformulated_body          text NULL — ≤500 chars; set on keep; internal only (used for clustering prompt)
├── embedding                  vector(384)  — embedding of `reformulated_title + "\n" + reformulated_body`; cosine
├── embedded_at                tstz NULL — timestamp of the embedding pass (for reprocessing)
└── moderation_note            text NULL — operator-free-text when hidden/spam (post-hoc correction)

feedback_cluster
├── id                     ULID PK
├── summary                text — one-line label (≤120 chars), set by the clustering agent
├── description            text NULL — optional 2-3 sentence elaboration
├── summary_embedding      vector(384) — embedding of `summary + "\n" + (description ?? "")`; cosine
├── submission_count       int — denormalised count of non-hidden, non-rejected members
├── first_submitted_at     tstz
├── last_submitted_at      tstz
├── visibility             text — hidden | visible | promoted (default visible)
├── lifecycle              text — new | acknowledged | planned | in-progress | shipped | declined
├── response               text NULL — operator-posted public reply
├── external_ref           text NULL — opaque (e.g. a Beads id or a GitHub issue URL)
└── updated_at             tstz

feedback_vote
├── id                     ULID PK
├── cluster_id             ulid FK
├── voter_user_hash        bytea(16) — app-derived HMAC; must match the cookie's user_hash
├── voter_workspace_hash   bytea(16) — opaque workspace HMAC
├── direction              int — +1 | -1
└── voted_at               tstz
UNIQUE (cluster_id, voter_user_hash)

used_token_nonce
├── nonce                  text PK — ULID from a verified magic-link token
├── user_hash              bytea(16)
└── expires_at             tstz — exp + 30s; pruned every 15 min

cluster_run
├── id                     ULID PK
├── started_at             tstz
├── finished_at            tstz NULL
├── kind                   text — sync | batch_cluster | batch_merge | reprocess
├── batch_size             int
├── moderate_model         text NULL — echoed from app response
├── embed_model            text NULL — echoed from app response
├── cluster_model          text NULL — echoed from app response
├── status                 text — running | ok | error
├── error                  text NULL
└── llm_cost_usd           numeric NULL — sum across the moderate + embed + cluster calls

mod_action
├── id                     ULID PK
├── action                 text — e.g. clusters.set, clusters.merge, submissions.unreject, users.ban, workspaces.unban, submissions.force_keep
├── target_type            text — submission | cluster | user_hash | workspace_hash
├── target_id              text — ULID for submission/cluster, hex hash for user/workspace
├── actor                  text — operator identifier (CLI invocation handle / OS user)
├── reason                 text NULL — operator-supplied free text
├── payload                jsonb NULL — command-specific extras (e.g. {"src":"...","dst":"..."} for merge)
└── at                     tstz

banned_user_hash
├── user_hash              bytea(16) PK — same shape as feedback_submission.submitter_user_hash
├── reason                 text NULL — operator-supplied free text
├── banned_at              tstz
└── banned_by              text — operator identifier, mirrors mod_action.actor

banned_workspace_hash
├── workspace_hash         bytea(16) PK — same shape as feedback_submission.submitter_workspace_hash
├── reason                 text NULL
├── banned_at              tstz
└── banned_by              text
```

Every row in `banned_user_hash` / `banned_workspace_hash` is paired
with a `mod_action` row written in the same transaction (`action =
users.ban` / `users.unban` / `workspaces.ban` / `workspaces.unban`);
unbans delete the `banned_*` row and write a balancing audit entry,
so the audit table is the single source of truth for ban history.

- **Pseudonymous from day 1.** `submitter_user_hash` and
  `submitter_workspace_hash` are pre-computed by the app before
  the token is signed (§ "Auth flow — Hash derivation"). The
  site never sees ULIDs, emails, or workspace slugs.
- **No `source` column.** Every submission comes from an
  authenticated app user via the magic-link handoff; there is no
  other origin.
- **No IP fields.** The site does not log request IPs on the
  submit/vote path; Caddy's access log strips the client IP to
  its `/24` prefix so rough geographic debugging still works
  without a tracing vector.
- **`body` is stored after the regex redaction pass** in "PII
  posture" — email addresses, phone numbers, long digit strings,
  URLs are rewritten to placeholders before the row is inserted.
  It is then fed to the agent pipeline; from that point on, only
  the agent's `reformulated_title` / `reformulated_body` are used
  in prompts and public rendering.
- **Embeddings are 384-dimensional `float32`** under v1, matching
  the seeded embedding model (see §03 and app §11). The dimension
  is a deployment-time choice; changing it requires a migration
  that re-embeds every submission and cluster. The RPC contract
  (§03) includes the dimension in the response so the site can
  detect a silent mismatch.
- **Vector index**: SQLite uses a `sqlite-vec` virtual table
  keyed by `id`; Postgres uses a `vector_cosine_ops` IVF index.
  Either way the site-api code paths only see a single SQL layer
  via SQLAlchemy + the dialect-specific adapter.

## Agent pipeline

Every submission walks the same three stages before it lands on
the board. Each stage is an HTTP call from `site/api/` to the app
(§03). The site owns orchestration; the app owns the LLM plumbing
and the embedding model.

```
                      ┌─────────────────────────┐
submit ──────────────▶│ 1. moderate + reformulate
                      │   + embed (one RPC)    │
                      └────────────┬────────────┘
                                   │
                    reject │ keep  │
                    ┌──────┴──────┬┘
            status='rejected'     │
                                  ▼
                      ┌─────────────────────────┐
                      │ 2. retrieve top-K       │
                      │   candidate clusters    │ ← local vector search
                      │   by cosine similarity  │
                      └────────────┬────────────┘
                                   │
                                   ▼
                      ┌─────────────────────────┐
                      │ 3. cluster (one RPC)    │
                      │   assign to existing or │
                      │   propose new cluster   │
                      └────────────┬────────────┘
                                   │
                                   ▼
                           store + surface
```

### Stage 1 — moderate + reformulate + embed

One RPC: `POST /_internal/feedback/moderate` (§03). Input is the
raw redacted body, category, and source. Output is a single
verdict plus (on keep) the reformulated text pair and the
embedding vector.

**Moderation categories** — the agent may emit exactly one
`moderation_reason` when rejecting:

| reason | what it catches | example |
|--------|-----------------|---------|
| `abuse` | slurs, harassment, threats against identifiable people | "<slur> at the ops team" |
| `gibberish` | fewer than 5 real words in any supported language | "asdf asdf asdf fjkdlsa" |
| `nsfw` | explicit sexual content, graphic violence, illegal content | (not reproduced) |
| `off_topic` | obvious advertising, SEO link bait, unrelated political rants | "Check out my SEO consulting services at <url>" |

Everything else — including terse venters, non-native English
prose, grammar errors, unclear ideas — lands as `keep` and
proceeds. The agent errs on the side of keeping. A false-positive
`reject` is recoverable via operator un-reject; a false-positive
`keep` is recoverable via operator `hide`. Both are cheap. The
enum is deliberately small: there is no separate scripted-flood /
copy-paste-spam reason because authenticated rate limits and the
agent's own clustering pass cover that surface without a dedicated
moderation verdict.

**Reformulation** — on `keep`, the agent returns:

- `reformulated_title` — one crisp line (≤ 120 chars). No emoji,
  no trailing punctuation, no "Feature request: " prefix. Neutral
  imperative or noun phrase: "Let the agent assign tasks based on
  room type". This is what the board shows.
- `reformulated_body` — cleaned-up 2-3 sentence paraphrase (≤ 500
  chars) in the submission's detected language. Used for the
  clustering prompt in stage 3 and for operator review via CLI.
  Never rendered publicly.
- `detected_language` — two-letter ISO code.

The agent does **not** invent content. If the submission is
unclear, the title captures the uncertainty (e.g. "Unclear request
related to scheduling") rather than making up specifics.

**Embedding** — the agent returns a 384-dim `float32` vector of
`reformulated_title + "\n" + reformulated_body`. Normalised to
unit length so cosine similarity = dot product. v1 default model
is a locally-run `BAAI/bge-small-en-v1.5` via the app's
`feedback.embed` capability (app §11); larger / hosted models can
be swapped in without changing this spec. v1 concatenates title
and body with a single newline; if real-corpus recall trends
body-heavy (a sparse title plus a long body drowning out the
title's signal), a known-tunable next pass is to repeat the title
2-3× before the body to bias the embedding toward it. Treat as a
recall lever, not a v1 change.

Stage 1 is **synchronous** on submit, under a 3 s budget. Timeouts
or non-200 responses → the submission is stored with
`status='pending'` and a later retry sweep picks it up. The
visitor's `/thanks` page is unchanged — they never learn about
timing. **Rejected submissions reach `/thanks` with the same copy
as pending and kept ones**: the submitter is never told they were
rejected, and operator un-reject (§ "CLI surface") is the only
recovery path.

### Stage 2 — retrieve top-K candidate clusters

A local SQL query. No RPC.

```
SELECT id, summary, description
FROM feedback_cluster
WHERE visibility IN ('visible', 'promoted')
ORDER BY 1 - (summary_embedding <=> :query_embedding)   -- pgvector / sqlite-vec syntax
LIMIT :k
```

- `k = 8` by default. Tuned to balance prompt size against
  recall. Configurable via `SITE_CLUSTER_CANDIDATES_K`.
- Below a cosine similarity of `0.15`, the candidate is dropped
  from the payload regardless of `k` — including an obviously
  unrelated cluster just wastes tokens.
- If the table is empty (no existing clusters), stage 2 returns
  an empty list and stage 3 is forced to propose `new_cluster`.

### Stage 3 — cluster assignment

RPC: `POST /_internal/feedback/cluster` (§03). Input is the
reformulated pair plus the top-K candidates. Output is either an
existing `cluster_id` (from the candidates) or `new_cluster` with
a `new_summary`, `new_description`, and `new_summary_embedding`.

- The LLM sees **only the top-K candidates**, not the entire
  cluster set. The site has already pre-filtered with embeddings,
  so the prompt stays bounded no matter how many clusters exist.
- If the LLM picks a `cluster_id` not in the candidate list, the
  site rejects the response as malformed and retries once with
  `force_existing_only=false`. Persistent failure → the
  submission lands as `status='pending'`.
- **`new_cluster` creation is gated by confidence, not by
  a time-based rate limit.** The cluster RPC response carries a
  `confidence ∈ [0,1]`; the site requires `confidence ≥
  SITE_NEW_CLUSTER_MIN_CONFIDENCE` (default `0.65`) for any
  `new_cluster` assignment to be accepted. Below threshold the
  site re-asks with `force_existing_only=true` and takes the
  agent's best existing-cluster pick. This replaces an earlier
  "1 new cluster per hour globally" safety valve that was redundant
  once submits are authenticated and per-user rate-limited: the
  real flood-of-noise-clusters scenario required anonymous writes.

Stage 3 runs synchronously on submit; budget 2 s. Stage 2 + 3
together under 3 s means the `/thanks` page can confidently show
"Your idea is grouped with: <cluster summary>" for the common
case.

**End-to-end submit budget.** Stage 1 (3 s) + stage 3 (2 s) + the
local stage-2 query and database write give a worst-case sync
budget of ~5 s, with another ~1 s of headroom for network jitter.
The submit form is a POST with an optimistic loading indicator
(spinner + "Posting your idea — this can take a few seconds");
the React island never blocks the UI thread and the indicator
must tolerate a tail latency up to ~6 s before it gives up and
shows the timeout copy.

### Scheduled batch re-cluster

Every 6 hours a worker pulls up to 200 submissions with
`status='pending'` (typically from stage-1 or stage-3 timeouts)
and drives them through the pipeline as above. It also takes an
opportunistic pass at **cluster merges**:

- Pulls all pairs of visible clusters with cosine similarity ≥
  `0.82` between `summary_embedding`s.
- Sends the pairs to the cluster RPC in its merge-check mode (a
  request whose body carries a `check_merges` array of
  `{src_id, dst_id, src_summary, dst_summary}` items, no
  `submissions`/`candidates`; see §03 "Endpoint 3"). The app
  returns a `merges` array with a confidence per pair. Above
  `0.85` confidence, the site performs the merge (same semantics
  as `site-admin clusters merge`).
- Merge is **append-only**: member submissions move from source
  to dest; source cluster flips `visibility='hidden'` but is
  never deleted. The audit row survives.

Batch run summaries are written to `cluster_run` with `kind='batch_cluster'`
or `kind='batch_merge'` so operators can monitor cost and
throughput without reading logs.

## Public board

Rendered statically at build time (with a short ISR-like rebuild
every 10 minutes) plus a React island for live vote updates.

- Default sort: `submission_count DESC, last_submitted_at DESC`.
- Filter chips: `idea | bug | question | all` and `any lifecycle
  | active | shipped | declined`.
- Search: client-side `fuzzy` over cluster summaries; no
  server-side search in v1.
- Each cluster card shows: summary, submission count, vote total,
  lifecycle pill, last-updated relative timestamp. Clicking the
  card routes to `/suggest/cluster/<id>`.
- Empty state: "No ideas yet — be the first." with a pointer at
  the form.
- `visibility='hidden'` clusters never render. `visibility='promoted'`
  clusters pin above the sort order, max 3.

### Cluster detail page

- Summary, description (if any), operator response (if any),
  lifecycle pill.
- "People also said" — the **reformulated titles** of the five
  most recent non-rejected, non-hidden submissions in the cluster.
  One line each, agent-authored. Verbatim bodies are never shown;
  neither is any timestamp more precise than the date, nor any
  attribution.
- Vote widget (see below).
- "Submit your own idea" link back to `/suggest`.

The reformulated title is the public unit of content. The
original verbatim `body` is stored (for operator review via CLI
and for re-running the pipeline if the agent changes) but no
public route renders it.

## Voting

- One vote per cluster per authenticated user. Vote is `+1`
  (useful) or `-1` (not interesting). No neutral.
- Requires the `__Host-suggest_session` cookie; without it the
  vote endpoint returns `401 unauthenticated` and the button
  renders as a "Log in to vote" CTA.
- Votes are rate-limited to **30 per user per hour**. Above that
  the endpoint returns `429`.
- Re-voting on the same cluster with a different direction
  replaces the existing row (UPSERT on `(cluster_id,
  voter_user_hash)`); the visitor can flip their vote without
  penalty.
- Vote total shown on cards is `sum(direction)`; never the raw
  up/down split. Keeps the UX a signal, not a referendum.

## Lifecycle and operator moderation

The agent does first-pass moderation (§ "Agent pipeline", stage
1). The operator is a safety net, not a queue walker. Their job is
to catch the agent's false-positive rejects and false-positive
keeps, and to steer cluster lifecycle.

### CLI surface

```
site-admin clusters list [--lifecycle <state>] [--visibility <state>]
site-admin clusters show <id>
site-admin clusters set <id> --lifecycle acknowledged --response "..."
site-admin clusters set <id> --visibility promoted
site-admin clusters merge <src-id> <dst-id>
site-admin clusters split <id> <submission-id> [<submission-id>...]

site-admin submissions hide <id> [--reason "<free text>"]
site-admin submissions rejected-list [--reason <category>] [--since <ts>]
site-admin submissions unreject <id>              # clears reject, re-runs the pipeline
site-admin submissions reprocess <id>             # force-re-run the pipeline on a kept submission
site-admin submissions force-keep <id> --title "..." --body "..."  # operator authors reformulated text and skips the pipeline
site-admin submissions show <id>                  # shows verbatim body + reformulated pair + agent's reasoning

site-admin notify <cluster-id>                    # dispatches "your idea is now in progress" emails
```

- Every CLI action writes a row to a `mod_action` audit table
  (same structure as app audit; stored site-side).
- `merge` moves every member of `<src-id>` to `<dst-id>` and
  marks `<src-id>` as `visibility=hidden` (not deleted — keeps
  the audit trail).
- `split` pulls out listed submissions into a brand-new cluster
  whose summary is generated by a fresh single-item RPC call.
- `unreject` and `reprocess` re-run the full pipeline (moderation
  → embed → cluster) and **override** the `moderation_decision`
  if the agent's new call disagrees with the operator's
  instruction. The operator uses `submissions hide` if they want
  to keep a submission out of the board without trusting a
  re-run.
- `submissions hide --reason` writes free text into the
  `moderation_note` column. The `moderation_reason` enum (`abuse |
  gibberish | nsfw | off_topic`) is reserved for the agent; the
  operator's reasoning is never coerced into it. `--reason spam`,
  `--reason duplicate`, etc. are all just operator-flavoured notes.
- `submissions force-keep` is the operator escape hatch for a
  submission the agent persistently rejects (or that fails the
  pipeline entirely). It writes the operator-supplied
  `reformulated_title` / `reformulated_body` directly, sets
  `moderation_decision='keep'` and `status='accepted'`, and queues
  a `/embed` call to populate `embedding` so the next batch
  re-cluster pass can place the submission. Editing
  `moderation_decision` directly in the DB is **not** an escape
  hatch: such a row has no `reformulated_*` and no `embedding`,
  so the pipeline cannot cluster it until `reprocess` is run; the
  CLI is the supported path.

### The rejected-log

`site-admin submissions rejected-list` is the operator's daily-
weekly sanity check. It shows every `status='rejected'` submission
with its `moderation_reason`, the agent's one-line reasoning (see
§03 response shape), and a tail of the verbatim body. The
expected operator action is to scan for obvious false-positives
and `unreject` them. Everything not unrejected is assumed
correctly-classified crap and is never surfaced publicly.

A minimum-viable operator rhythm is **one weekly pass** through
`rejected-list --since $(date -d '7 days ago' -Iseconds)` (GNU
`date`; on a BSD/macOS host, `date -v-7d -Iseconds`); most rejects will look
obvious. If the list is ever empty or ever wildly inflated,
that's itself an operational signal the agent's prompt is
off-calibration.

No web moderation UI in v1. Adding one is a separate spec.

## PII posture

Submissions land from authenticated app users, but the site still
treats every word as potentially PII — users paste things they
shouldn't. Posture:

- **Body text is redacted before insert.** The pass runs in the
  site backend — no LLM call, only regex + well-known patterns:
  - Email addresses → `<email>`.
  - Phone numbers (ITU + common national shapes) → `<phone>`.
  - Runs of 10+ digits → `<number>`.
  - URLs → kept if the host is on a short allowlist
    (`github.com`, `crew.day`); otherwise `<url>`.
- **The site never learns the real user identity.** It receives
  pre-computed `submitter_user_hash` and `submitter_workspace_hash`
  values from the signed token (§ "Auth flow"). The app holds the
  salt that makes these hashes reversible; the site does not.
- **`notify_email` is stored only when the opt-in box is
  ticked.** The UI cannot pre-check it. The email is kept in a
  separate column, never rendered on the board, and never passed
  to the clustering RPC — the app never sees emails.
- **Board rendering never shows any user or workspace identifier,
  raw email, raw timestamp-to-the-second, or any string marked
  hidden/rejected/spam.** Only the agent-authored
  `reformulated_title` reaches the public surface. The
  "N people at your workspace also asked for this" line is a
  computed aggregate (COUNT DISTINCT `submitter_workspace_hash`
  matching the viewer's cookie); the underlying hashes are never
  exposed to the client.
- **The verbatim `body` is never rendered publicly** under any
  circumstance — not on the cluster card, not on the detail
  page, not in email. Only operators see it, via
  `site-admin submissions show`.
- **No IP logging.** Caddy access logs on the submit/vote paths
  truncate the client IP to its `/24` prefix; `site-api` itself
  logs nothing IP-shaped. The hash-based model replaces the IP-
  rate-limit infrastructure that earlier drafts required.

The site's privacy policy at `/legal/privacy` documents the
posture; §04 spells out the CSP and cookie posture that keeps the
site from accidentally adding tracking alongside.

## Email posture

- **Outgoing email is opt-in only.** The visitor must tick the
  box to give an address and agree to be emailed.
- Emails are sent only by operator-triggered `site-admin notify
  <cluster-id>` — never automated. Content is a plain-text
  summary of the cluster's new lifecycle state plus the
  operator's response. No tracking pixel, no campaign; ordinary
  transactional mail via SMTP.
- Visitor can opt out via a signed unsubscribe link in the
  footer of any such email. Unsubscribe flips a `notify_suppressed`
  flag on every submission with that email.
- No welcome / digest / marketing mail of any kind. The site is
  not a newsletter.

## Rate limits and abuse controls

Every write carries an auth cookie bound to `submitter_user_hash`
and `submitter_workspace_hash`. Limits key off those, not off IP:

- **Submit:** 10 per `submitter_user_hash` per hour; 50 per day.
- **Vote:** 30 per `voter_user_hash` per hour.
- **Per-workspace rail:** 100 submits per `submitter_workspace_hash`
  per day. Stops a single compromised or over-enthusiastic
  workspace from dominating the board.
- **Magic-link handshakes:** 20 per user per hour (the app side
  enforces this on `/feedback-redirect`; the site accepts
  verified tokens without an extra rail).
- **Payload cap:** 4 KB per field, 32 KB per request body.
- **Per-user duplicate detection:** identical body (post-redaction,
  case-folded, whitespace-normalised) from the same
  `submitter_user_hash` in the last 24 h returns
  `200 {duplicate: true, cluster_id: <id> | null}` without writing a
  new submission. `cluster_id` is the cluster the original
  submission joined, when it has one. The `/thanks` page shows
  "You already submitted this idea — it's grouped with
  <cluster summary>" (or, if the original is still `pending`,
  "We already have your idea — it's queued for grouping"). With
  authenticated submits there is no anonymity to protect, so the
  earlier silent-drop is replaced by an honest response.
- **Ban list:** `site-admin users ban <user_hash>` flips an
  entry in a `banned_user_hash` table; subsequent tokens for
  that hash verify fine but the submit/vote endpoints return
  `403 banned`. `site-admin workspaces ban <workspace_hash>`
  does the same at workspace granularity. The CLI is the only
  surface for this — no automation in v1. Every ban and every
  unban writes a `mod_action` row in the same transaction as the
  `banned_*` table mutation, so the audit trail captures both
  directions even after the `banned_*` row is deleted.

Excess returns `429` with a `Retry-After` header. No captcha, no
`SITE_BLOCK_CIDR`, no IP-hash infrastructure — authentication is
doing that work now.

## Edge cases

| What happens when … | Answer |
|---------------------|--------|
| an unauthenticated visitor hits the submit endpoint directly | `401 unauthenticated`. The form on `/suggest` wouldn't have rendered, so this is either a scripted probe or someone who expired mid-draft. The UI kicks the user back to the "Log in to submit" CTA. |
| a verified token is replayed | `410 token_already_used`. The nonce lives in `used_token_nonce` until its `exp + 30s`. The UI sends the user back to the app for a fresh mint. |
| a user's auth cookie expires mid-draft | Submit returns `401`; the island swaps in a "Your session timed out — log in to submit this" card with the draft preserved in localStorage. |
| the visitor submits while the app is down | Stage 1 times out; submission lands with `status='pending'`. The batch pass re-runs the full pipeline when the app is back. `/thanks` shows the generic "We group ideas every few hours" line. |
| the agent rejects a genuine piece of feedback | Operator un-rejects via `site-admin submissions unreject <id>`. The pipeline re-runs; if the agent still rejects, the operator runs `site-admin submissions force-keep <id> --title "..." --body "..."` (§ "CLI surface") to author the reformulated text directly and skip the agent. |
| the agent keeps something the operator considers crap | `site-admin submissions hide <id> --reason spam`. Cluster counts refresh on the next card render. |
| the agent proposes `new_cluster` with `confidence < 0.65` | The site re-asks with `force_existing_only=true`. If the best candidate still fails `min_confidence_existing=0.5`, the submission lands as `pending` and the batch pass re-tries 6 h later. No hard "1 per hour" quota. |
| the agent returns a `cluster_id` not in the top-K candidates | The site rejects the response as malformed, retries once with `force_existing_only=false` (letting the agent propose `new_cluster`), and on persistent failure queues the submission as `pending`. |
| two concurrent submissions both trigger a `new_cluster` proposal for the same theme | A per-minute advisory lock on "new-cluster proposals" forces one to win; the loser falls through to `pending`. The batch merge pass picks up any near-duplicate that slips through the lock window. |
| an operator merges `src` into `dst` but `dst` later gets split | Audit trail keeps the history; member submissions follow the latest assignment. No retroactive re-computation. |
| a submission's body is redacted to empty | Treated as `<0 chars after redaction>` → rejected client-side before submit; the server also rejects with a generic "Please give a bit more detail." |
| the visitor types PII into the body anyway (free-text personal info) | The redaction pass catches the well-known patterns; the agent sees only the redacted text. The verbatim body is never rendered publicly (only shown to the operator via CLI). |
| the embedding model is swapped to a different dimension | All existing rows need re-embedding. A `site-admin reembed --model <new>` command is shipped alongside the model change; it runs the batch through the new model and rewrites the vector column in one transaction per batch of 500. Dimension mismatch is detected at boot and boot fails until the migration is complete. |
| the app is on a new `feedback.moderate` version that rejects more aggressively | No schema change needed; the operator audits `rejected-list` and, if there's a pattern, raises it with the app team. Past decisions are not re-evaluated unless the operator explicitly `reprocess`es. |
| a cluster reaches thousands of submissions | Board renders the card the same way; the detail page's "People also said" stays at five. The count alone tells the story. |
| a lifecycle moves to `shipped` or `declined` | Card gains a pill and, if `response` is set, renders the operator's one-line reply inline. Voting stays open (it's a signal for "more like this"). |
| the site is self-hosted alongside the app (rare case) | Everything still works, provided the operator has turned on the three `feedback.*` capabilities. Budgets are deployment-scope (app §11). |

## Cross-references

- §03 App integration — for the `moderate`, `embed`, and
  `cluster` RPC shapes and the `CREWDAY_FEEDBACK_URL` link.
- §04 Deployment and security — for the CSP, rate-limit
  enforcement tier, and the "no tracking" privacy contract.
- App §11 — the three deployment-scope capabilities
  (`feedback.moderate`, `feedback.embed`, `feedback.cluster`),
  their model assignments, and the `embeddings` capability tag
  introduced for the embedding model.
- App §18 — the i18n seam the form and board share.
