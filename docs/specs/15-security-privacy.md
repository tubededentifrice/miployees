# 15 — Security and privacy

## Threat model

### Assets

| Asset                                          | Sensitivity |
|-----------------------------------------------|-------------|
| User credentials / sessions (all grant roles) | Critical    |
| API tokens (agent auth)                        | Critical    |
| Property access data (door codes, wifi)        | High        |
| User personal info (legal name, pay rate)      | High        |
| User payout details (IBAN, PAN, wallet)        | Critical    |
| Payroll / expense amounts                      | High        |
| Guest names and stay dates                     | Medium      |
| Task history / completion evidence             | Medium      |
| Asset documents (invoices, warranties)         | Medium      |
| Instructions content                           | Medium      |
| Photos of interiors / guest areas              | High        |

### Adversaries

- **Opportunistic Internet scanner.** Script kiddies scanning open
  ports and default creds. Mitigated by: no default creds, no listener
  on public IPs by default, TLS-only, passkey-only auth.
- **Phisher / social engineer.** Tricks staff into logging into a
  look-alike. Mitigated by: passkeys (phishing-resistant).
- **Lost / stolen user phone.** Mitigated by: owner or manager can
  revoke; passkey requires user verification (biometric).
- **Ex-staff.** Mitigated by: off-boarding revokes role grants,
  credentials, and sessions.
- **Leaked agent token.** Mitigated by: delegated tokens inherit user
  permissions but are separately revocable, per-token audit with full
  conversation tracing (`agent_conversation_ref`), optional IP
  allow-lists, shorter default TTL (30 days), automatic deactivation
  when the delegating user is archived. Scoped tokens additionally
  limited by explicit scopes.
- **Hostile LLM prompt injection** (a task note or receipt contents
  trying to hijack the assistant). Mitigated by: structured-output
  schemas, tool-call whitelists, never executing untrusted content as
  action, redaction layer.
- **Database exfiltration.** Mitigated by: secrets envelope-encrypted;
  passwords not stored; token secrets hashed (argon2id).
- **Malicious guest.** Welcome token scoped read-only to one stay.

### Out of scope

- Nation-state adversary.
- Side-channel attacks on the host.
- Physical security of the server.

## TLS

- **Mandatory** on any environment reached from outside `localhost` /
  Tailscale. The compose recipe ships with Caddy for automatic
  Let's Encrypt.
- HSTS (`max-age=31536000; includeSubDomains; preload` once the
  manager opts in).
- TLS 1.2+ only.

## Binding policy

The guard is deliberately simple and explicit. There is no
environment-sniffing, no container-detection, no CIDR-based trust of
generic ranges. We want one knob so that an operator can audit it at
a glance.

**Rule:** at start-up, the server resolves `CREWDAY_BIND` to a set
of concrete addresses and applies this check:

1. Loopback (`127.0.0.0/8`, `::1`) always passes.
2. The server enumerates local network interfaces. `CREWDAY_
   TRUSTED_INTERFACES` is a comma-separated list of `fnmatch`-style
   globs of interface names (default `tailscale*`). If **every**
   address the bind resolves to is assigned to an interface whose
   name matches at least one glob in the list, the bind passes. The
   interface name is what makes it trustworthy — not the address
   range.
3. `0.0.0.0` and `::` never pass on their own; they always need the
   opt-in below, because they inherently bind every interface
   regardless of trust.
4. Any other bind (a non-loopback IPv4/IPv6, a hostname resolving to
   one) requires `CREWDAY_ALLOW_PUBLIC_BIND=1`.

We deliberately do **not** trust the CGNAT range (`100.64.0.0/10`)
by CIDR: it is used by ISP carrier-grade NAT, mobile carriers, and
shared-IP VPS providers. An address there is not necessarily on a
Tailscale interface, and misreading one as "private" would default-
open the service to every other subscriber on the same NAT. The
interface-name check avoids that class of mistake.

The default (`tailscale*`) covers the standard Tailscale device name.
Operators on alternative mesh overlays (nebula, headscale with a
renamed interface, wireguard) override the env var to list their own
globs — e.g. `tailscale*,wg*,nebula*`. The baseline is **not**
additive: setting the env var replaces the default, so an operator
who wants to also trust `wg0` must set
`CREWDAY_TRUSTED_INTERFACES=tailscale*,wg*` explicitly. We chose
replace-semantics so the configured value is a complete, auditable
list in one place rather than a delta against an invisible baseline.

We do not try to decide whether a `0.0.0.0` bind is "really" public.
Container escapes, `--network=host`, and misconfigured orchestrators
all make that guess unreliable — and a wrong guess is a default-open
service on the Internet. The operator must confirm the bind is safe
for their environment by setting the opt-in.

### Per-deployment guidance

- **Bare-metal / VM:** leave the default. Reverse-proxy via a local
  Caddy/Nginx that binds the Internet-facing port.
- **Tailscale:** set `CREWDAY_BIND` to the node's Tailscale IP; no
  opt-in needed, because the default `tailscale*` glob matches. If
  the mesh interface has a different name, override
  `CREWDAY_TRUSTED_INTERFACES` with the full list you want trusted
  (remember the default is replaced, not extended).
- **Single-container (§16 recipe A):** set `CREWDAY_BIND=0.0.0.0:8000`
  inside the container and `CREWDAY_ALLOW_PUBLIC_BIND=1`. External
  reachability is then gated by the host-side Docker port map
  (`ports: ["127.0.0.1:8000:8000"]`), which the operator inspects
  directly.
- **Compose full-stack (§16 recipe B):** same two env vars on the app
  service; the compose network has no published port for `app`, so
  reachability is strictly Caddy → `app:8000` on the internal bridge.

The opt-in therefore always appears **alongside** the port-map
configuration in the same file, so an operator reviewing the compose
file sees both the "I know this binds wide" admission and the
"and here is how reachability is actually limited" evidence together.

See §16 for deployment details.

## HTTP security headers

- `Content-Security-Policy`: strict — `default-src 'self'`, no
  inline scripts except a single hashed bootstrap, no `unsafe-eval`,
  `frame-ancestors 'none'`, `form-action 'self'`, `base-uri 'self'`,
  `img-src 'self' data:` (for small icons); uploaded images served
  from the same origin under `/files/*/blob`.
- `Strict-Transport-Security` (once HSTS opted in).
- `Referrer-Policy: strict-origin-when-cross-origin`.
- `Permissions-Policy`: allow `camera=(self)` only on worker pages
  (for task evidence). `geolocation` is **not** granted anywhere in
  v1 (the v0 clock-in geofence is gone — see §09 "Out of scope").
- `X-Content-Type-Options: nosniff`.
- `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-
  Policy: same-origin`.

## Cookies

Session cookie `__Host-crewday_session`:
- `Secure; HttpOnly; SameSite=Lax; Path=/`.
- Value opaque (192-bit random) → DB lookup.

CSRF cookie `crewday_csrf` + `X-CSRF` header on non-GET (double-
submit).

## Secrets management

### Secret envelope

A per-workspace AES-256-GCM key, itself encrypted by the host's
**root key** (`CREWDAY_ROOT_KEY`, 32 bytes base64). The root key is:

- **Single-container:** read from env on start-up, never written to
  disk.
- **Compose:** read from a docker secret (`/run/secrets/…`).

Every secret (OpenRouter API key, SMTP password, iCal feed URL
containing tokens, property wifi password, property access codes,
**full payout account numbers** — see §09) is stored as
`secret_envelope` with per-row nonce. Decryption paths are
deliberately narrow:

1. **Payout manifest** (HTTP, §09) — owner/manager passkey session
   only; on §11's interactive-session-only list; not stored; not
   cached by the idempotency layer.
2. **Envelope-key rotation** (host CLI, §15 below) — no HTTP
   surface; authorised by host shell access; plaintext never leaves
   the server process.

Bearer tokens (scoped or delegated) cannot reach either path. For
(1), the approval pipeline would persist the decrypted response in
`agent_action.result_json` — so the endpoint is refused outright
(see §11 "Interactive-session-only endpoints"). For (2), there is
no endpoint at all (see §11 "Host-CLI-only administrative
commands").

The stored payslip PDF and all API responses use only `display_stub`.

```
secret_envelope
├── id
├── owner_entity_kind/id       # property, ical_feed, workspace setting
├── purpose                    # free slug
├── ciphertext                 # bytes
├── nonce                      # bytes
├── key_fp                     # 8-byte BLOB; first 8 bytes of SHA-256(root_key)
├── created_at, rotated_at
```

**Key fingerprint.** On encrypt, the current root key's 8-byte
fingerprint (`SHA256(root_key)[:8]`) is stamped into `key_fp`. On
decrypt, the code checks `envelope.key_fp` against the fingerprint
of whichever root key is about to be used; a mismatch raises
`KeyFingerprintMismatch` with a clear, actionable message — for
example, `"envelope encrypted with key fingerprint abc123; current
key is def456. Restore the correct key or re-encrypt."` — rather
than returning garbage bytes or a generic AES-GCM tag-mismatch
error. The fingerprint is short enough to be useless as an oracle
(8 bytes ≈ 2^64 collision space for a random preimage) and long
enough to distinguish the handful of keys a deployment actually
sees in a rotation.

### Key rotation

`crewday admin rotate-root-key` decrypts every envelope with the
old key and re-encrypts with the new. Bounded progress reporter.
**Host-CLI only** — there is no HTTP endpoint for this operation,
and therefore no agent path (see §11 "Host-CLI-only administrative
commands"). The command writes its own `audit_log` rows directly
as `system` actor with `via = 'cli'`.

The new key material **never appears in argv**. The only accepted
sources are:

- `--new-key-file <path>`: reads 32 bytes (base64 or raw) from the
  file. The command refuses to run if the file is not regular, is
  world- or group-readable (mode must be `0600`), or is owned by a
  user other than the one running the command.
- `--new-key-stdin`: reads the key from stdin. Must be attached to
  a pipe or redirected file (not a TTY). The command refuses to run
  if stdin is a TTY — an operator typing the key into an interactive
  prompt would end up echoed to the terminal.

If the legacy `--new <value>` form is passed, the command exits
immediately with a non-zero status and a message explaining that
argv-delivered keys leak into shell history, `ps aux`, journald,
and `docker exec` command tracking, and pointing at the two safe
forms above. The CLI argv itself is never logged by the command
(the argv parser redacts any positional after `--new` to
`<withheld>` in its own diagnostics, on the off chance the flag
got through an outer wrapper).

On success the command zero-fills the in-memory copy of the new key
before exiting. It does **not** write the key anywhere on disk; the
operator is responsible for storing it in their secret manager and,
on the next service start, supplying it via `CREWDAY_ROOT_KEY`
(single-container) or the Docker secret at `/run/secrets/...`
(compose) — the same mechanisms used for the initial key (§16).

Example safe invocations:

```
# Key file produced by a secret manager, never on a shell line:
install -m 0600 /dev/null /tmp/newkey        # create the file with correct mode first
<your secret manager writes to /tmp/newkey>  # e.g., op read "op://prod/crewday/root"
crewday admin rotate-root-key --new-key-file /tmp/newkey
shred -u /tmp/newkey

# Piped from a secret manager, no shell history:
op read "op://prod/crewday/root" | crewday admin rotate-root-key --new-key-stdin
```

### Root key compromise playbook

Root-key exposure is a P0 incident. The command surface above
plus `key_fp` (see "Secret envelope") give operators a deterministic
recovery path. The playbook is:

1. **Rotate immediately.** `crewday admin rotate-root-key
   --new-key-file /secure/path` generates a new key, stamps it
   active, and keeps the compromised key in a `legacy_keys` slot
   for 72 hours. Envelopes still carrying the old `key_fp` decrypt
   under the legacy key during the transition so scheduled jobs
   (payouts, iCal polls, SMTP sends) do not break mid-rotation.
2. **Re-encrypt.** `crewday admin rotate-root-key --reencrypt`
   starts a background worker that walks every `secret_envelope`
   row, decrypts with whichever key's fingerprint matches
   (`envelope.key_fp`), and re-encrypts under the new key with
   the new fingerprint. Progress is logged to
   `audit.key_rotation.progress`; the worker is resumable — if
   interrupted, it picks up where it left off by filtering on
   `key_fp = <old>`.
3. **Finalise.** After `SELECT COUNT(*) FROM secret_envelope
   WHERE key_fp = <old_fp>` returns 0 **and** the 72-hour window
   has elapsed, `crewday admin rotate-root-key --finalize` removes
   the legacy key slot. Any subsequent decryption attempt against
   a legacy-fingerprint envelope fails loudly with
   `KeyFingerprintMismatch`; this is the cryptographic signal that
   any leaked key is now useless. The finalise step writes
   `audit.key_rotation.finalized`.
4. **Restore-with-wrong-key guard.** `crewday admin restore`
   refuses to start if any envelope in the backup carries a
   `key_fp` not matching the current root key or a loaded legacy
   key. The operator is prompted to supply additional keys via
   `--legacy-key-file <path>` (repeatable) until every fingerprint
   resolves; otherwise the restore aborts before any rows land in
   the target database. No silent corruption, no half-encrypted
   restore.

**Threat model note.** The 72-hour legacy window is deliberate:
it trades a continued exposure risk for operational continuity.
During that window, anyone with the leaked key **and** database
access can still decrypt old envelopes. The re-encrypt worker
(step 2) is the thing that actually makes the leak historical —
operators MUST watch it to completion and not shorten the window
until step 3 prerequisites are met. A shorter window is available
(`--finalize-now`) for operators who can tolerate a brief outage
and prefer to minimise exposure; it refuses unless step 2 has
completed.

### Token hashing

API token and magic-link tokens stored as **argon2id** hashes.
`token_hash_params` stored alongside to support parameter upgrades.

## Passkey specifics

See §03 for ceremonies. Additional hardening:

- RP ID is strictly bound to the configured hostname; no wildcard.
- `userVerification: required` on both registration and assertion.
- `userHandle` is a per-person random 32-byte blob (not the email),
  so the hostname + userHandle pair does not reveal user identity.
- **Assertion sign-count rollback auto-revokes the credential.**
  FIDO2's sign-count exists precisely to detect clones; ignoring
  the signal while logging it is the worst-of-both-worlds posture.
  On the first rollback event for any credential, the server
  **hard-deletes** the `passkey_credential` row on a fresh
  Unit-of-Work (the primary UoW rolls back with the raised
  `CloneDetected`; a fresh UoW keeps the revocation from
  disappearing with the rollback), rejecting any subsequent auth
  ceremony with the credential id with the same
  `401 invalid_credential` envelope as an unknown credential. Two
  audit rows land: `audit.passkey.cloned_detected` (the detection
  event, carrying rolled-back counter values) and
  `audit.passkey.auto_revoked` (the revocation event, carrying
  `reason: "clone_detected"`). Every session for the credential's
  owner is invalidated with cause `clone_detected` (§"Session-
  invalidation causes") and the owner plus every workspace owner
  is notified through the shared audit view (§10 messaging).
  The user recovers via §03's lost-device flow (magic link → new
  passkey enrolment); if the original credential is still in the
  user's possession, re-enrolling it is a 30-second flow on the
  same device.

  Revocation is a hard delete, not a tombstone: `passkey_credential`
  carries no `deleted_at` column — the forensic trail lives
  entirely in `audit_log`. Revocation is therefore irreversible —
  there is no "false-positive restore" path. Password-manager bugs
  that cause the sign-count to decrement are vanishingly rare; the
  cost of a false-positive revocation (30 seconds to re-enrol) is
  tiny compared to the cost of a false-negative (a cloned
  credential remains live). Deployments that want to accept that
  tradeoff for other reasons MAY flip
  `settings.auth.passkey_rollback_auto_revoke = false`, which
  falls back to the v0 behaviour (alert only, no revoke) and
  carries an operator warning in `/admin/alerts`.

## Rate limiting and abuse controls

- Auth endpoints: 10/min per IP for login begin; 5/min per IP for
  magic-link send.
- Magic-link consumption: 3 failed attempts → 10-minute IP lockout.
- API: 600 req/min per token default; 60 req/min for `llm.call`.
- Guest welcome page: 30 req/min per token.
- Non-auth health endpoints: unlimited but with expensive DB calls
  deferred.

## Input validation

- Pydantic v2 everywhere. Strict mode for IDs and enums.
- All `Location` / `Content-Disposition` / redirect targets built
  from whitelists, never from user input.
- File uploads:
  - Max size configurable per purpose (default 10 MB images, 25 MB
    PDFs).
  - MIME sniffed server-side; we trust the sniff, not the header.
  - Image re-encoding: uploaded JPEGs are re-encoded to strip EXIF
    and GPS unless the workspace sets `retain_exif=true` on that
    purpose. **Avatars (`users.avatar_file_id`) always strip EXIF,
    regardless of the workspace override**, and are normalised to
    a 512×512 WebP (§12 `POST /me/avatar`).
  - PDFs are not re-encoded but are scanned for embedded scripts via
    a small `pdfid` wrapper; scripted PDFs are rejected.
- SQL via SQLAlchemy ORM; no string concat.

### Blob download authorization

Files are content-addressed by SHA-256, but the download URL is
**not** a bearer token. Every `GET /uploads/<hash>` goes through an
authorization middleware that looks up the `upload` row (§02 `file`)
joining `<hash>` to a workspace the requester has access to under
the current session's `WorkspaceContext`. If no such row exists for
the viewer's workspace, the middleware returns `404 blob_not_found`
— never `403` — so an attacker cannot distinguish "hash does not
exist" from "hash exists but belongs to another workspace"
(aligned with §"Constant-time cross-tenant responses"). The SHA-256
path stays the stable content address; deduplication across
workspaces works at the blob-bytes level, but the `file` row
(which carries `workspace_id`) is the authorization anchor.

For guest welcome pages and client portals where the consumer is
unauthenticated, the server mints a **short-lived signed URL** at
page-render time:

```
/uploads/signed/<hash>?e=<unix_expiry>&s=<hex_signature>
```

with `signature = HMAC-SHA256(server-signing-key, hash || "." ||
expiry || "." || guest_token_id)`. Default expiry is 15 minutes;
the signed-URL route is parallel to `/uploads/<hash>` and ignores
session cookies. The authenticated `/uploads/<hash>` route, in
turn, ignores any `e` / `s` query parameters and always performs
the session-based authorization check — so a URL cannot "upgrade"
from guest-signed to session-bearer by path rewriting. Signed URLs
are single-scope (one blob, one guest token) and are logged
(`audit.upload.signed_url_issued`) for review.

## SSRF

Any server-side fetch whose URL is operator- or user-supplied
(iCal feeds §04, outbound webhooks §10, future LLM-tool
fetches §11) MUST run through the shared fetch-guard module
`app/net/fetch_guard.py`, which enforces:

- `https://` scheme only; all other schemes rejected.
- Host resolution pinned: the caller resolves the host once, the
  resolved address is checked against the private-range blocklist
  (loopback, RFC 1918, link-local, multicast, reserved, `0.0.0.0`),
  and the TCP connection opens to the pinned address with no
  subsequent re-resolution.
- Certificate validation mandatory; self-signed only when the
  caller opts in per-deployment (`settings.*_allow_self_signed`).
- Redirect policy: same-origin only (scheme+host+port); cross-
  origin redirects abort.
- Hard limits: per-call body cap, connect + read timeouts,
  per-feature monthly budgets. Callers override the defaults only
  upward to stricter values.

Per-feature sections (§04 "SSRF guard", §10 "Webhooks
(outbound)") specify the exact limits and error surface for
their fetches; they inherit these defaults from the guard
module.

## Logging and redaction

- Logs JSON-structured via `structlog`.
- A **redaction filter** runs on the root logger:
  - `Authorization`, `Cookie`, `Set-Cookie` headers → `<redacted>`.
  - Anything matching `password|token|secret|cookie|account_number|
    account_number_plaintext|pan|iban` at any depth of a log dict →
    `<redacted>`.
  - Regex-match-redact common PII (email, phone, IBAN, PAN Luhn-like
    16-digit sequences) in free-text fields.
- Request logging: method, path, status, duration, actor id, token
  id, correlation id. **Never the body.**

## Audit log

Append-only, see §02. Guaranteed invariants:

- One row per state change, in the same transaction.
- `before_json` / `after_json` pass through the same redaction filter
  as logs before storage.
- Worker job `audit_integrity_check` runs daily, verifying
  monotonic ULID ordering and no gaps in `correlation_id` blocks.

### Tamper detection

Every `audit_log` row carries a `prev_hash` and a `row_hash` column
(§02). Together they form a per-deployment hash chain. The worker
job `audit_verify` walks the log ordered by `id`, recomputes each
row's hash, and compares with the stored `row_hash`. Any mismatch
raises an `audit.tamper_detected` alert (highest severity) with the
first bad row's id and halts further workspace-admin actions
(`crewday admin *`) until an operator investigates via
`crewday admin audit verify`.

The verifier runs nightly and is also invoked inline at the start
of every `crewday admin audit export` so an exported archive is
never produced from a silently corrupted tail.

This is **tamper-evident, not tamper-proof**: a DB-level attacker
who also has the hash-chain code can rewrite history consistently
— recompute every `row_hash` and `prev_hash` downstream of the
forged row. The chain raises the bar to "someone who can also
rewrite the application code", which is the realistic threat model
for a self-host deployment. Operators concerned about a stronger
guarantee should periodically pin the latest `row_hash` off-box
(printed receipt, off-site notary, witness workspace); the design
does not require it, and v1 does not ship it.

## Privacy and data rights

Even though this is self-hosted, GDPR-like practices apply because
much of the data is personal.

**Data-minimisation note: no clock GPS.** v1 deliberately drops
the v0 `geofence_required` setting and the `shift.geo_in_*` /
`shift.geo_out_*` columns — bookings (§09) are the time record,
and a per-tap GPS coordinate added no commercial signal beyond
what task-completion timestamps already provide. The PII surface
is correspondingly smaller: no historical worker location data,
no Geolocation API consent prompt on clock-in, nothing to redact
on `crewday admin purge`.

**Labour-law compliance.** A booking row plus its
`actual_minutes` (when amended) constitutes a compliant time
record under FR / EU rules: it captures `scheduled_start`,
`scheduled_end`, `actual_minutes_paid`, the worker, the property,
and a manager-verifiable audit trail. The worker is not required
to perform minute-by-minute self-reporting. Jurisdictions
requiring per-day signatures need a future export (flagged in
§19).

- **Access export**: any user can request their own data as JSON +
  attached files — `POST /api/v1/me/export` queues a file; email
  delivery when ready.
- **Right to rectification**: users can update their own profile
  fields (§05).
- **Right to erasure**: owner/manager-triggered; `crewday admin
  purge --person <id>` anonymizes the user row (name/email/phone
  nulled) and scrubs free-text fields in their tasks, comments,
  bookings, expenses. Financial rows retain amounts and dates (legal
  retention trumps erasure for payroll).

  Payout-specific erasure steps (§09):

  - Delete the `secret_envelope` rows referenced by the user's
    `payout_destination` rows.
  - Clear `display_stub`, `secret_ref_id`, `country`, `label` on
    those rows (keep `id`, `kind`, `currency`, timestamps so FK
    references in historical payslip snapshots do not break).
  - Scrub `payslip.payout_snapshot_json`: retain `destination_id`,
    `kind`, `currency`, and `amount_cents`; blank out `label` and
    `display_stub`. The accounting trail (who was paid how much)
    survives; routing identifiers do not.
  - **Payslip PDFs (`payslip.pdf_file_id`) are already safe**: they
    are rendered from the snapshot at issue time and never contain
    full account numbers. No rewrite needed.
  - Subsequent calls to `POST /payslips/{id}/payout_manifest` for
    any affected payslip return 410 Gone: the routing data is gone
    on purpose.
- **Data portability**: CSV exports of timesheets, payslips, and
  expenses (§09).
- **Retention defaults** (see §02 for the canonical table):
  - `audit_log`: 2 years.
  - `session`: 90 days after revocation.
  - `llm_call`: 90 days.
  - `email_delivery`: 90 days.
  - `webhook_delivery`: 90 days.

  All configurable per workspace. The worker job
  `rotate_operational_logs` runs daily and applies the current
  retention to every table listed above; archived rows land in
  `$DATA_DIR/archive/<table>.jsonl.gz`.

## Row-level security (RLS)

The tenancy seam for row-level security is **`workspace_id`** (v0
used `household_id`; the rename is covered in §02 "Migration").
v1 is multi-tenant from day 1 (§00 G11, §01 "Multi-tenancy
runtime"), so isolation is load-bearing on every deployment that
holds more than one workspace — on any backend.

Isolation is enforced in two layers:

1. **Application layer (always on, every backend).** Every
   repository call filters by `ctx.workspace_id` from the active
   `WorkspaceContext` (§01). The filter is auto-injected by the
   ORM-level hook described in §01 "Tenant filter enforcement" —
   that section is the primary implementation reference; the
   import-boundary gate + per-repository tenant regression test
   (§17) keep it honest. It runs identically on SQLite and
   Postgres.
2. **Database layer (capability `features.rls` — Postgres only).**
   Every workspace-scoped table also carries an RLS policy that
   restricts `SELECT / UPDATE / DELETE` to
   `current_setting('crewday.workspace_id')`. The `tenancy`
   module (§01) sets that session variable at the start of every
   transaction from the active `WorkspaceContext`; missing it is
   a programming error that trips a `SET LOCAL` sentinel and
   aborts the transaction. Policies gate rows by `workspace_id`;
   they do not distinguish `grant_role` — that is enforced at
   the application layer (see §02 `role_grants`). RLS is
   **defence-in-depth** — the safety net when a context forgets
   the app-level filter.

SQLite deployments lack the capability and therefore run on the
application-layer filter alone. The cross-tenant regression test
(§17) runs on both backends; any repository that passes on
Postgres (where RLS would mask a bug) but fails on SQLite (where
only the app filter stands) fails CI.

For deployments where the adversary model includes other tenants
on the same instance (open self-serve SaaS with untrusted
tenants), Postgres is the recommended backend because the
defence-in-depth layer is meaningful. For single-organisation
self-host (trusted tenants, e.g. one family with multiple
workspaces), SQLite with the app-level filter is acceptable.
Neither is mode-gated; both are supported by the same code.

### Cross-tenant regression test

The §17 gate `tenant_isolation_cross_workspace` seeds two
workspaces and, for every workspace-scoped repository method,
verifies that a caller authenticated in workspace A cannot read,
write, or soft-delete a row owned by workspace B — on both
SQLite and Postgres. A failure fails CI; adding a new
repository method without extending the gate is caught by a
parity check.

Users with membership in more than one workspace (§02
`user_workspace`) pick an active workspace via the
`/select-workspace` picker (§14); the chosen workspace id rides
with every subsequent request as the URL slug. Switching
workspaces re-seeds the RLS context and is audited.

### Constant-time cross-tenant responses

For any workspace-scoped resource URL, the `404 not_found`
response when the resource does not exist MUST be
**byte-identical and timing-identical (±5 ms)** to the
`404 not_found` when the resource exists but belongs to a
different workspace. We never return `403` — distinguishing
"doesn't exist" from "exists but not yours" leaks enough to
enumerate workspace slugs, task ids, property ids, user ids,
and every other opaque identifier across tenants.

Implementation: the auth middleware performs the workspace
scope check **before** the resource lookup. On a workspace
mismatch, the middleware issues a deterministic
`time.sleep()`-padded response using the **same code path**
as the nonexistent case (same `404` envelope, same headers,
same response body shape; the padding uses a pre-computed
uniform draw from the observed lookup-time distribution so
timing bands overlap without leaking through the padding
distribution itself). No branch in the response path depends
on whether the row exists under another workspace.

The error envelope is the shared `not_found` shape defined in
§12 — never `forbidden_cross_workspace` or any variant that
reveals scope. Both the "missing" and "exists-elsewhere" branches
emit byte-identical bodies by routing through the same helper.
Logging records the real reason internally for operator
diagnostics (`audit.tenant.cross_scope_miss`); the wire response
does not.

§17 tenant-isolation test suite asserts:

- byte-identical error envelope for both the "missing" and
  "exists-elsewhere" cases across a sample of 100 endpoints;
- timing bands overlap (±5 ms) across the same sample under
  a steady-load harness;
- no `403` is ever returned from a workspace-scoped URL.

### Shared-origin XSS containment

Path-prefix addressing (§01) means every workspace on a given
deployment shares a single browser origin. Script that executes
in one workspace has same-origin access to every workspace the
current user is logged into on that deployment — cookies,
IndexedDB, Cache Storage, and `postMessage` are all shared. This
is tolerable for a self-hosted deployment (trusted tenants, one
org) and a hard threat for SaaS with open self-serve signup
(strangers). The defences below are mandatory on SaaS and
recommended on self-host.

- **Strict default CSP.** `default-src 'self'`, `script-src
  'self' 'nonce-<per-request>'`, `style-src 'self' 'nonce-…'`,
  `img-src 'self' data: blob:`, `connect-src 'self'`,
  `frame-ancestors 'none'`, `form-action 'self'`. No inline
  scripts or styles without a nonce. Emitted on every HTML
  response by FastAPI middleware.
- **Sanitiser on every UGC render.** Instructions (§07), task
  comments (§06), expense descriptions, agent-preferences blobs
  (§11), and guest-welcome overrides (§04) pass through
  `bleach` (Python, server-side) with a whitelist of block
  tags, no `<script>`, no `<iframe>`, no `javascript:` URLs, no
  event-handler attributes. React renders the sanitised HTML
  via `dangerouslySetInnerHTML` only from server output; never
  from user input client-side.
- **No `eval` on imported external content.** iCal bodies, OCR
  receipts, and email inbound hooks are parsed with structured
  parsers only — no regex-to-template-eval shortcuts. Same rule
  for LLM-returned JSON: parsed and schema-validated, never
  executed.
- **`__Host-` cookies, origin-locked.** Session cookies use the
  `__Host-` prefix (Secure, HttpOnly, SameSite=Lax, Path=/), so
  they cannot be narrowed by path. Workspace scope is carried
  by the URL path + server-side `user_workspace` check, not by
  cookies.
- **Subresource integrity.** Any third-party JS bundle served
  (v1 goal: none) must carry `integrity="sha384-…"`. CI fails
  a build that introduces a `<script src="https://...">`
  without SRI.
- **Postmessage allowlist.** The one intentional cross-frame
  boundary (guest welcome iframe; demo-mode embedder, §24) uses
  explicit `targetOrigin` checks on both sides. Any other
  `postMessage` listener in the SPA is a lint error.

### Session-invalidation causes

Credential-population changes (and a few other surgical security
events) flip every matching session row to invalidated rather than
deleting it — `invalidated_at` + `invalidation_cause` are stamped
so `validate()` refuses the cookie while the forensic trail
survives (§02 `session`, `app/auth/session.py` `invalidate_for_user`
/ `invalidate_for_credential`). Every call site emits a single
`audit.session.invalidated` row carrying the cause and the count.

The complete catalogue of causes the codebase emits today — any new
cause MUST land here in the same PR as its call site so operators
retain a single lookup table:

| `invalidation_cause` | Emitted by | Scope | Notes |
|----------------------|------------|-------|-------|
| `passkey_registered` | `register_finish` (app/auth/passkey.py) | per-user | Credential-population change (§03 "Additional passkeys"). The domain function receives `user_id` but not the caller's session PK, so every session for the user is invalidated — including the caller's own; the SPA re-auths after the ceremony. The signup sibling `register_finish_signup` does **not** emit this cause: it creates the user's first credential on a brand-new account with no prior sessions to invalidate. `complete_recovery` also funnels through `register_finish`, so the recovery path emits one `recovery_consumed` audit (non-zero count) followed by a trailing `passkey_registered` audit (zero count — the sessions were already flipped). |
| `passkey_revoked`    | `revoke_passkey` (app/auth/passkey.py) | per-user | User-initiated revoke via `DELETE /auth/passkey/{credential_id}` (§03 "Additional passkeys", §12). The caller's own session is invalidated with the rest — a credential-revocation needs a clean re-auth. |
| `recovery_consumed`  | `complete_recovery` (app/auth/recovery.py), via the internal `_invalidate_sessions` helper | per-user | Recovery re-enrolment (§03 "Re-enrollment side-effects" / "Self-service lost-device recovery"). No caller session to preserve — the ceremony runs on a device with no prior session for this user. |
| `clone_detected`     | `post_login_finish` route handler's `except CloneDetected` branch (app/api/v1/auth/passkey.py), via `_invalidate_for_credential_fresh_uow` → `invalidate_for_credential` | per-credential-owner | §"Passkey specifics" "sign-count rollback auto-revoke". The domain `login_finish` raises `CloneDetected`; the router handler catches it and runs the invalidate on a **fresh** UoW because the primary UoW rolls back on the raise — otherwise the suspected-stolen sessions would stay live. The session-invalidation step is emitted once here; a sibling fresh-UoW call (`_auto_revoke_credential_fresh_uow`) then hard-deletes the `passkey_credential` row and writes `audit.passkey.auto_revoked` with `reason: "clone_detected"`. The two steps are intentionally separate so each audit row has exactly one meaning. |

### Self-serve abuse mitigations

Open self-serve signup (§03, §00 G12) exposes any deployment that
runs it to a new threat class: adversaries provisioning throwaway
workspaces to burn LLM budget, enumerate the deployment, or abuse
outbound email. The following gates apply whenever
`settings.signup_enabled = true` (operator-settable, §01
"Capability registry"); they are not deploy-mode-specific.

- **Rate limits on `POST /api/v1/signup/start`:**
  - ≤ 5 successful starts per source IP per hour.
  - ≤ 3 successful starts per email lifetime on the deployment.
  - ≤ 200 signup starts per deployment per hour (global
    cool-off). Exceeded limits return `429` with a retry-after
    header.
- **Disposable-domain blocklist** on the email-domain portion of
  the submitted address. The list ships with a default set
  (the `disposable-email-domains` dataset, pinned release);
  operators override via the deployment setting
  `settings.signup_disposable_domains_path` (§01 "Capability
  registry"). A blocked domain returns `400 disposable_email`
  with copy inviting the user to use a different address.

  **Freshness.** The bundled list MUST be refreshed weekly by a
  CI job `refresh-disposable-domains.yml` that pulls from the
  pinned upstream dataset, regenerates the in-repo file
  (`app/abuse/data/disposable_domains.txt`), and opens a PR.
  The first line of the file is a pinned reference date comment
  (`# generated 2026-04-14`) the CI build checks against: if
  the in-repo dataset is more than **30 days old** (comment
  date vs. build date), CI fails the build with a clear
  "disposable-domain list stale; merge the open refresh PR"
  error so drift does not compound. Operators override per
  domain using `crewday admin allow-email-domain <domain>`
  (§13), which writes to a workspace- or deployment-scoped
  allowlist that takes precedence over the blocklist.
- **Magic-link TTL 15 min, one-use.** Links are single-consumption
  and invalidate on claim or on `/signup/start` retry for the
  same `(email, desired_slug)`.
- **Tight caps pre-verification.** Workspaces with
  `verification_state ∈ {unverified, email_verified}` have:
  - LLM budget: 10% of the free-tier cap.
  - Upload quota: 25 MB.
  - Outbound email: 10 messages lifetime (invitations, notifications).
  - No outbound webhooks (§12), no iCal polling (§05), no
    integration-events transport.
  Caps lift on `human_verified` (see §02 `workspace`).
- **Abuse signals written to audit log** and surfaced on the
  operator-only `/admin/signups` page: burst-rate trips, same
  IP across distinct emails, repeat provisioning from one email,
  quota near-breach events.
- **Signup GC worker.** `signup_gc` runs every 15 minutes;
  removes stalled signup attempts (magic-link redeemed but
  passkey never registered) after 1 hour, and archives
  workspaces whose provisioning user never completed passkey
  registration after 24 hours.
- **No workspace enumeration.** `GET /w/<slug>/...` from an
  unauthenticated or non-member caller returns `404` uniformly,
  with a constant-time response so slug-probing can't time-
  fingerprint existence.

### Per-IP aggregate LLM spend cap

Per-workspace LLM budgets (§11) are bounded; an adversary can
still burn real money by provisioning `N` unverified workspaces
and summing the caps. To close that loop, every `workspace` row
stores `signup_ip` (captured at creation; IPv4 or IPv6). Every
`llm_call` row is already workspace-scoped (§11). The existing
llm-budget subsystem maintains a rolling **30-day sum of
`llm_call.cost_usd` across every workspace whose `signup_ip`
matches the same IP** (or /64 prefix for IPv6, since a single
household on IPv6 rotates `/128`s freely but holds a stable
`/64`).

Default policy: when the per-IP aggregate exceeds
`3 × workspace_llm_cap_usd_30d` (i.e. $15/month per IP with the
default $5 per-workspace cap), every subsequent LLM call from
any of those workspaces returns
`402 payment_required` with `error_code = ip_budget_exceeded`
and a user-facing banner:

> *"This IP address has exceeded its unverified-workspace spend
> limit. Verify one of your workspaces (the owner email address
> on file) to continue."*

**Verification** is an email-link round trip that reuses the
§03 magic-link machinery: the banner links to
`/w/<slug>/settings/verify-ownership`, which mails a magic
link to the owner's address; redemption promotes the workspace
out of the aggregate pool by setting
`workspace.verification_state = human_verified`. The
`signup_ip` value is retained for audit but no longer counts
against the cap. A single verified workspace is enough to
continue operating; sibling unverified workspaces under the
same IP still count against the multiplier until each is
verified on its own.

The multiplier (`3×`) and a per-IP hard ceiling
(`ip_llm_cap_hard_usd_30d`) are deployment-configurable via
`crewday admin signup set-ip-cap [--multiplier N]
[--hard-cap-usd N]`; neither persists into `workspace` rows,
both are stored in `deployment_setting` (§02).

**Shared egress handling.** The spec does **not** distinguish
VPN / Tor / corporate-NAT / dorm networks from residential IPs;
the `3×` multiplier is already generous for a small team on the
same egress, and operators who run crew.day on networks with
genuinely shared egress (a residence with an aggregator, a
corporate network provisioning dozens of family accounts) adjust
the multiplier upward or lean on the verification email path.
Operators who want to trade off more fairness for more cost risk
may wire a human-moderated allow path
(`crewday admin signup allow-ip <cidr>`) into the same setting.

Cross-refs: §03 "Self-serve signup" (`workspace.signup_ip`
capture); §11 "LLM budget" (`llm_call.cost_usd` source and the
existing per-workspace enforcement path).

### Self-service lost-device & email-change abuse mitigations

The self-service flows in §03 ("Self-service lost-device
recovery", "Self-service email change") introduce a second
email-anchored re-enrollment surface. The mitigations below apply
whether or not self-serve signup is enabled:

- **Rate limits on `POST /api/v1/auth/recover/start`:**
  - ≤ 3 successful starts per email per hour.
  - ≤ 10 starts per source IP per hour.
  - ≤ 200 starts per deployment per hour (global cool-off).
  All limits use constant-time responses; callers always see
  `200 {"status": "sent_if_exists"}` so neither a user's
  existence nor their step-up status can be probed. 429 is only
  returned once the per-IP or global cap trips.
- **Rate limits on `POST /api/v1/me/email/change_request`:**
  - ≤ 5 requests per user per hour.
  - ≤ 3 requests per source IP per minute (covers session
    hijack that fan-outs across users).
  A duplicate request for the same `(user_id, new_email)` within
  the magic link's 15-min TTL is idempotent — the server
  returns 202 without re-sending, to prevent mailbox flooding.
- **Recent re-enrollment cool-off.** Email-change requests made
  from a passkey session younger than **15 minutes** return
  `409 recent_reenrollment`. This bounds the attacker window on a
  hijacked recovery magic link.
- **Revert link TTL 72 h.** The informational notice to the old
  address carries a revert link whose token reverts the
  `users.email` swap once; after redemption or TTL, the notice is
  inert.
- **Audit always written.** `auth.recover.*` and
  `auth.email_change_*` events land in `audit_log` on every
  request (including misses and rate-limit trips), with
  `email_hash` and `ip_hash` only — no plaintext addresses — so
  the trail is usable for abuse response without leaking PII on
  export.
- **Step-up bypass is not a fallback.** If the user holds a
  `manager` or `owners` position anywhere and does not supply a
  valid break-glass code, no email is sent. The user is expected
  to use the manager-mediated `users.reissue_magic_link` path
  instead (§03). The response shape is identical to the success
  case.

### Personal task visibility

Tasks with `is_personal = true` (§06) are visible only to: (a) the
user identified by `task.created_by`, and (b) members of the `owners`
permission group for the workspace. Non-owner managers never see
personal tasks in listings, team dashboards, approval queues, reports,
or audit surfaces — the same `workspace_id` workspace-tenancy filter
applies, but an additional application-layer check enforces
`is_personal = false OR created_by = caller_id OR caller_is_owner`.
On Postgres this is an additional RLS predicate on the `tasks` table;
on SQLite it is enforced as a query-time filter in the ORM layer.

Workers may originate tasks via `tasks.create` (§05) but the quick-add
default is `is_personal = true` — sharing to team requires an explicit
opt-out by the creator before submission.

### Cross-workspace visibility (shared properties)

A property may be linked to more than one workspace via
`property_workspace` (§02, §04). A **non-owner** linked workspace
(`managed_workspace` or `observer_workspace`) sees a deliberately
narrower slice of the property's data than the owner workspace —
the **operational minimum** needed to dispatch or observe work.

The boundary is expressed by the
`property_workspace.share_guest_identity` flag (default `false`;
§02). When **false**, a non-owner workspace sees:

- Property: `name`, `timezone`, `country`, address city/region
  (not line1/line2/postal_code), `kind`.
- Unit: `name`, `max_guests`, default check-in/check-out times.
  Welcome-page fields (wifi password, door codes, house rules,
  emergency contacts, local tips) are **hidden**.
- Stay: `check_in_at`, `check_out_at`, `unit_id`, `guest_kind`,
  `guest_count` (if set), `status`. **Hidden**: `guest_name`,
  `guest_email`, `guest_phone_e164`, external channel id
  (iCal UID), per-stay notes authored on the owner side,
  welcome-page overrides.
- Tasks, bookings, work_orders, quotes, vendor_invoices: only the
  rows whose `workspace_id` matches the viewing workspace. A task
  created by workspace A on the shared property is invisible to
  workspace B.
- Files (§02): a file is visible to a non-owner workspace only if
  the file is attached to a row that workspace already sees under
  the rules above.

When **true** (the owner workspace explicitly widens the share on
the invite, §22 `property_workspace_invite.initial_share_settings_json`),
the guest identity fields (`guest_name`, `guest_email`,
`guest_phone_e164`) and the welcome-page personalisations become
visible to the non-owner workspace. Address line1/line2, iCal UID,
and owner-side notes stay redacted in either case — the agency
never needs the full street address to dispatch, and the iCal UID
is a secret by virtue of being a direct-fetch URL.

Implementation notes:

- On Postgres, the narrowing is an RLS policy on each affected
  table that joins `property_workspace` and selects fields
  conditionally on `share_guest_identity`.
- On SQLite (no RLS), the narrowing is enforced at the repository
  layer: every query that resolves a row belonging to a non-owner
  workspace passes through a `redact_for_cross_workspace(row,
  viewer_workspace_id)` filter that blanks the hidden fields and
  returns `null` for entire hidden files.
- The cross-tenant regression test (§"Cross-tenant regression
  test") is extended with a *cross-workspace* case: workspace B
  MUST NOT see guest name on a stay authored in workspace A
  unless `share_guest_identity = true`, on either backend.
- Audit rows (`audit_log`) written in workspace A are visible only
  to workspace A — audit never crosses workspace boundaries,
  regardless of `share_guest_identity`.

### Cross-workspace user identity

A `users` row can surface in workspace A's views even though the
user's primary workspace is B — for example a contractor authored
in B who also appears on a property A and B share via
`property_workspace` (§02, §22), or a worker who holds a role
grant in both workspaces. The user's full profile belongs to B;
A sees only what it needs to dispatch or observe.

When workspace A reads a `users` row whose authoring workspace is
B:

- `display_name` MUST be projected to **first name + last
  initial** (e.g. "Marie L."). The raw `display_name` column
  never ships cross-workspace.
- `email`, `phone_e164`, `address_*`, `full_legal_name`, and
  `bank_payout_detail_id` (plus any column cross-referenced to
  bank or tax PII) MUST be hidden — the serializer returns
  `null`, not an empty string, so clients cannot distinguish
  "absent" from "redacted" and infer presence.
- Avatar URL MAY be served if the user has opted in (default is
  hidden, matching the default of strict redaction below).

Workspace B's own reads are unchanged and see the full row.

Implementation is a server-side projection keyed off
`workspace_id`: the repository layer resolves the "home" workspace
for the user (`users.primary_workspace_id` or the union of the
user's non-client role grants) and compares it to the viewer's
`WorkspaceContext`. A raw `users` row is never serialised
cross-workspace — callers that bypass the projection fail the
tenant isolation tests (§17).

**Opt-in widening.** A user may grant full-name visibility to a
named peer workspace via a `user_workspace_visibility` preference
(future work — Beyond v1, §19). v1 ships with strict redaction as
the only behaviour; the preference surface is reserved but
inactive, so that adding the opt-in later is an additive change
rather than a flip of the default.

### Client rota visibility

The client portal scheduler (§14 "Client portal shell", §22) is a
read-only view onto `/api/v1/scheduler/calendar` scoped to the
caller's bound properties. To keep staff PII minimal, workers on
that feed serialise as **first name + `work_role.name`** only —
no last name, no email, no phone, no avatar URL. Tasks and rota
slots include the property, the weekday, and the local time
window; the client sees *who is booked when*, not where that
person lives or how to reach them. Contact channels to staff go
through the messaging surface (§10, §23) under the same existing
mediation, not through the rota view.

The serialisation is server-enforced on `GET
/scheduler/calendar` when the caller's active role grant is a
client binding (§22). A client who happens to also hold a manager
grant in the same workspace sees the manager-wide feed when they
switch roles, not the narrowed one.

## Off-app channel privacy (WhatsApp / SMS)

When off-app adapters are eventually enabled, WhatsApp and SMS
addresses stored on `chat_channel_binding` rows (§23) are **PII**.
They would back both agent reach-out (§10) and user-initiated agent
conversation (§23), and must be treated with the same care as
legal-name fields:

- Addresses are **redacted from upstream LLM prompts by default**.
  The redaction layer in §11 already handles `phone_e164` with
  tokenized hash substitution; the gateway's `address` column
  passes through the same filter, and the `address_hash` (HMAC-
  SHA256 with the workspace key) is what the runtime uses for
  O(1) inbound lookup. No LLM capability sees raw addresses; the
  channel adapter (Meta Cloud API, SMS gateway, Telegram Bot API)
  is called by the worker **outside** the LLM path, using the
  plaintext from the DB.
- Message bodies generated by the agent do pass through the LLM,
  but the body is produced first (in the target language, per §18)
  and then the worker attaches the recipient address as an envelope
  field — never as prompt context.
- Inbound messages (text, media, transcripts) are stored on
  `chat_message` with the same PII rules as any task comment; the
  sender's phone number is tokenized in any LLM call that reads
  the message.
- **Link challenge codes** (§23 "Link ceremony") are hashed
  (argon2id) and never logged in plaintext; the plaintext code
  exists only in the outbound WhatsApp body and is never stored.
- **Webhook signature verification** is mandatory on every inbound
  and uses the per-workspace webhook secret stored in
  `secret_envelope`; a missing or invalid signature returns 401
  before any DB read or write.
- On GDPR erasure (§ "Right to erasure"), `chat_channel_binding`
  rows for the user are revoked and scrubbed (`address`,
  `address_hash`, `provider_metadata_json`, `display_label` set
  to null; `id`, `channel_kind`, timestamps retained so the audit
  trail survives). `chat_message.provider_message_id`,
  `body_md`, `body_md_original`, `file_ids`, and
  `language_original` are nulled on the user's own rows; the
  `kind`, `direction`, and timestamps remain.

When these adapters ship, workspace owners should be able to turn off
off-app reach-out globally or per user. For the user, the opt-out
is **unlinking the binding** — there is no separate preference
toggle; no active binding means no off-app reach-out (§10, §23).
A `STOP` keyword reply on any bound address must flip the binding
to `revoked` immediately (§23).

## LLM data handling

See §11 redaction details. Additional rules:

- The workspace's LLM settings page shows exactly which
  capabilities send data upstream, which fields are redacted, and a
  per-capability toggle to **disable upstream entirely** (turns the
  feature off).
- Instruction bodies sent upstream only when the capability requires
  grounding (default on). Toggle exists.
- Uploaded photos sent upstream for `expenses.autofill` only; never
  for `tasks.assist`.
- No PII is sent in telemetry. There is no product telemetry in v1.
- **Agent preferences are a PII opt-in seam** (§11). The redaction
  layer is deliberately bypassed for preference bodies so authors
  can reference real people and places; in exchange, the save
  endpoint refuses bodies that match hard-drop secret patterns
  (IBAN, access codes, Wi-Fi passwords, API tokens) and the
  editor shows a "sent to the model as written" banner. No other
  free-text field gets this carve-out.
- **Extracted document text** (§02 `file_extraction`, §21 "Document
  text extraction") goes through the same redaction layer as other
  free-text fields. Hard-drop secret patterns are stripped from
  `body_text` before the row is indexed or returned by `read_doc`,
  and the row is flagged `has_secret_marker = true` so the
  document UI warns the operator. The original binary on
  `file.storage_key` is **not** modified — only the extracted
  text. Extraction never sends bytes upstream unless the
  `documents.ocr` capability is assigned to a vision model and
  local OCR returned no usable text; in that case the per-call
  budget envelope and the per-capability redaction rules apply
  as for any other LLM call.
- **Extraction worker isolation.** The `extract_document` worker
  runs in a subprocess with no network egress except the
  configured LLM provider endpoint. The only system binaries
  invoked are `tesseract` and `pdftotext`; both are pinned via
  the container image's package manifest (§16). PDF script
  rejection (existing in §15 "MIME sniffing") still applies — a
  PDF that fails the script check never reaches the extraction
  worker.
- **System docs** (§02 `agent_doc`) are operator-shipped Markdown.
  They are **not** redacted on the way to the model and are not
  workspace-scoped — the editor banner reminds operators not to
  paste workspace secrets, customer data, or live API keys.

## Backup / restore security

- Backups include the DB, `data/files/` (§02 `file` entity storage
  root), and the encrypted `secret_envelope` rows.
- **The root key is not included** in backups. Restoring a backup to
  a new host requires supplying the same root key; otherwise envelopes
  cannot be decrypted and the operator is warned before the server
  starts so they can recover the key or rotate.
- Backup bundle is optionally encrypted with a passphrase (AES-256-
  GCM over zstd-compressed tar).

## Dependency supply chain

- `uv` lockfile committed.
- `osv-scanner` in CI; findings are blockers (§17).
- Dependabot / Renovate for weekly PRs.
- SBOM (CycloneDX) generated in release artifacts.
- Container images built with Python 3.12 slim + pinned digests.
- No `pip install` at runtime.

## Incident response

- Security contact in `SECURITY.md` (TBD per-deployment).
- Break-glass path (§03) documented and tested quarterly.
- `audit export` + `backup` before any destructive incident action.

## Demo deployment

The demo deployment (§24) relaxes a handful of the headers and cookie
flags above, and adds a set of demo-specific abuse controls. None of
these changes apply to prod or staging — they are gated on
`CREWDAY_DEMO_MODE=1` and the demo deployment refuses to boot if that
flag is set on a container whose `CREWDAY_PUBLIC_URL` is not on the
demo allowlist.

### CSP on demo

The demo app must be embeddable in a landing page that lives at a
different origin. `frame-ancestors` is therefore an **allowlist**
rather than `'none'`:

- `frame-ancestors` = whitespace-separated value of
  `CREWDAY_DEMO_FRAME_ANCESTORS`; default empty → demo runs stand-
  alone (no embedding). Operators set it to the landing origins they
  intend to embed from (e.g. `https://crew.day https://*.crew.day`).
- `X-Frame-Options` is not set on demo responses — the CSP
  `frame-ancestors` directive supersedes and the legacy header cannot
  express an allowlist of multiple origins.
- Every other CSP directive is unchanged from the prod baseline
  above: `default-src 'self'`, no inline scripts except the hashed
  bootstrap, no `unsafe-eval`, `form-action 'self'`, `base-uri
  'self'`, `img-src 'self' data:`.

### Cookies on demo

- The production session cookie `__Host-crewday_session` is **not
  issued** on demo — passkey sessions do not exist there (§03 "Demo
  sessions").
- A single cookie `__Host-crewday_demo` is issued per visitor.
  Flags: `Secure; HttpOnly; SameSite=None; Path=/; Partitioned;
  Max-Age=2592000` (30 days).
- `SameSite=None` is required because the demo app is loaded inside
  an iframe whose top-frame is a different origin. `Partitioned`
  opts the cookie into CHIPS (Cookies Having Independent Partitioned
  State): its storage is keyed to the `(top-frame-origin,
  cookie-origin)` pair, so a different landing page embedding the
  same demo iframe gets a separate partition and therefore a
  separate workspace. That is the desired behaviour — each landing
  context is a separate playground.
- The `crewday_csrf` double-submit cookie is **not issued** on demo.
  The demo accepts non-GET requests when: (a) the demo cookie's
  signature verifies, (b) the request's `Sec-Fetch-Site` is
  `same-origin` or `same-site`, and (c) the workspace named by the
  cookie is still alive. All demo writes are scoped to the bound
  workspace and cannot touch secrets, payroll, or other workspaces,
  so the existing CSRF surface is an overreach.

### Bind policy

Unchanged. The demo container follows the same §16 recipe (bind to
`0.0.0.0:8000` inside the container, expose via the Docker port map or
a Caddy reverse proxy, never to the public interface directly).

### Rate-limiting and abuse controls on demo

In addition to the general rate limits above, the demo deployment
enforces (see §24 "Abuse controls"):

- Mint throttle: 10 new demo workspaces per IP per hour.
- Mutation rate: 60 writes per workspace per minute.
- LLM rate: 10 chat turns per workspace per minute.
- Upload cap: 5 MiB per file, 10 files per workspace lifetime,
  25 MiB per IP per day.
- Text-field cap: 32 KiB per field.
- Optional IP deny-list via `CREWDAY_DEMO_BLOCK_CIDR`.

### Secret posture on demo

- The demo deployment carries its **own** OpenRouter key, distinct
  from prod. Revoking it has no effect on any prod deployment.
- The demo deployment has its **own** `CREWDAY_ROOT_KEY`; envelope
  decryption paths are exercised on demo only for seed-time
  fixture setup (e.g., fake iCal feed URL pretending to be a
  secret). No real secret ever enters a demo `secret_envelope` row.
- The budget-refusal path (§11 "Workspace usage budget") does not
  decrypt or log any secret material; `budget_exceeded` is a pre-
  flight refusal that never reaches the provider.

### Privacy posture on demo

- Email addresses collected by demo "invite user" flows are stored on
  the `users` row for UI consistency, with the local part masked at
  render time (`a*@e*.com`). Addresses live exactly as long as the
  demo workspace (24 h rolling from last activity; §24).
- The redaction layer (§11) stays on; agent prompts on demo go through
  the same filter as prod.
- No product telemetry is emitted from demo. Access logs and audit
  rows stay local to the operator's stack.

## Items deferred but noted

- WebAuthn signed enterprise attestation inspection.
- OIDC as an alternative manager auth method.
- SOC 2 / HIPAA posture (out of scope for a workspace tool).
- End-to-end encryption of task content between owners/managers and
  workers (we operate server-side; any E2EE would defeat the agent
  layer).
