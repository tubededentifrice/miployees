# 15 — Security and privacy

## Threat model

### Assets

| Asset                                          | Sensitivity |
|-----------------------------------------------|-------------|
| Household manager credentials / sessions       | Critical    |
| Employee credentials / sessions                | Critical    |
| API tokens (agent auth)                        | Critical    |
| Property access data (door codes, wifi)        | High        |
| Employee personal info (legal name, pay rate)  | High        |
| Employee payout details (IBAN, PAN, wallet)    | Critical    |
| Payroll / expense amounts                      | High        |
| Guest names and stay dates                     | Medium      |
| Task history / completion evidence             | Medium      |
| Instructions content                           | Medium      |
| Photos of interiors / guest areas              | High        |

### Adversaries

- **Opportunistic Internet scanner.** Script kiddies scanning open
  ports and default creds. Mitigated by: no default creds, no listener
  on public IPs by default, TLS-only, passkey-only auth.
- **Phisher / social engineer.** Tricks staff into logging into a
  look-alike. Mitigated by: passkeys (phishing-resistant).
- **Lost / stolen employee phone.** Mitigated by: manager can revoke;
  passkey requires user verification (biometric).
- **Ex-employee.** Mitigated by: off-boarding revokes credentials and
  sessions.
- **Leaked agent token.** Mitigated by: token scopes, per-token audit,
  easy revocation, optional IP allow-lists.
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

**Rule:** at start-up, the server resolves `MIPLOYEES_BIND` to a set
of concrete addresses and applies this check:

1. Loopback (`127.0.0.0/8`, `::1`) always passes.
2. The server enumerates local network interfaces. `MIPLOYEES_
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
   one) requires `MIPLOYEES_ALLOW_PUBLIC_BIND=1`.

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
`MIPLOYEES_TRUSTED_INTERFACES=tailscale*,wg*` explicitly. We chose
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
- **Tailscale:** set `MIPLOYEES_BIND` to the node's Tailscale IP; no
  opt-in needed, because the default `tailscale*` glob matches. If
  the mesh interface has a different name, override
  `MIPLOYEES_TRUSTED_INTERFACES` with the full list you want trusted
  (remember the default is replaced, not extended).
- **Single-container (§16 recipe A):** set `MIPLOYEES_BIND=0.0.0.0:8000`
  inside the container and `MIPLOYEES_ALLOW_PUBLIC_BIND=1`. External
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
- `Permissions-Policy`: allow `camera=(self)` only on employee pages
  (for evidence), `geolocation=(self)` only on clock-in page.
- `X-Content-Type-Options: nosniff`.
- `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-
  Policy: same-origin`.

## Cookies

Session cookie `__Host-miployees_session`:
- `Secure; HttpOnly; SameSite=Lax; Path=/`.
- Value opaque (192-bit random) → DB lookup.

CSRF cookie `miployees_csrf` + `X-CSRF` header on non-GET (double-
submit).

## Secrets management

### Secret envelope

A per-household AES-256-GCM key, itself encrypted by the host's
**root key** (`MIPLOYEES_ROOT_KEY`, 32 bytes base64). The root key is:

- **Single-container:** read from env on start-up, never written to
  disk.
- **Compose:** read from a docker secret (`/run/secrets/…`).

Every secret (OpenRouter API key, SMTP password, iCal feed URL
containing tokens, property wifi password, property access codes,
**full payout account numbers** — see §09) is stored as
`secret_envelope` with per-row nonce. Decryption paths are
deliberately narrow:

1. **Payout manifest** (HTTP, §09) — manager passkey session only;
   on §11's never-agent list; not stored; not cached by the
   idempotency layer.
2. **Envelope-key rotation** (host CLI, §15 below) — no HTTP
   surface; authorised by host shell access; plaintext never leaves
   the server process.

Agent tokens cannot reach either path. For (1), the approval
pipeline would persist the decrypted response in
`agent_action.result_json` — so the endpoint is refused outright
(see §11 "Never-agent endpoints"). For (2), there is no endpoint
at all (see §11 "Host-CLI-only administrative commands").

The stored payslip PDF and all API responses use only `display_stub`.

```
secret_envelope
├── id
├── owner_entity_kind/id       # property, ical_feed, household setting
├── purpose                    # free slug
├── ciphertext                 # bytes
├── nonce                      # bytes
├── created_at, rotated_at
```

### Key rotation

`miployees admin rotate-root-key --new <base64>` decrypts every
envelope with the old key and re-encrypts with the new. Bounded
progress reporter. **Host-CLI only** — there is no HTTP endpoint for
this operation, and therefore no agent path (see §11 "Host-CLI-only
administrative commands"). The operator authorises the rotation by
virtue of having shell access to the deployment host and supplying
the new key material on the command line. The command writes its
own `audit_log` rows directly as `system` actor with
`via = 'cli'`.

### Token hashing

API token and magic-link tokens stored as **argon2id** hashes.
`token_hash_params` stored alongside to support parameter upgrades.

## Passkey specifics

See §03 for ceremonies. Additional hardening:

- RP ID is strictly bound to the configured hostname; no wildcard.
- `userVerification: required` on both registration and assertion.
- `userHandle` is a per-person random 32-byte blob (not the email),
  so the hostname + userHandle pair does not reveal user identity.
- Assertion sign-count decreases trigger an alert (`webauthn.rollback`
  audit event). Does not auto-revoke, but surfaces in digest.

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
    and GPS unless the household sets `retain_exif=true` on that
    purpose.
  - PDFs are not re-encoded but are scanned for embedded scripts via
    a small `pdfid` wrapper; scripted PDFs are rejected.
- SQL via SQLAlchemy ORM; no string concat.

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

## Privacy and data rights

Even though this is self-hosted, GDPR-like practices apply because
much of the data is personal.

- **Access export**: any person can request their own data as JSON +
  attached files — `POST /api/v1/me/export` queues a file; email
  delivery when ready.
- **Right to rectification**: employees can update profile fields
  (§05).
- **Right to erasure**: manager-triggered; `miployees admin purge
  --person <id>` anonymizes the person row (name/email/phone nulled)
  and scrubs free-text fields in their tasks, comments, shifts,
  expenses. Financial rows retain amounts and dates (legal retention
  trumps erasure for payroll).

  Payout-specific erasure steps (§09):

  - Delete the `secret_envelope` rows referenced by the person's
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

  All configurable per household. The worker job
  `rotate_operational_logs` runs daily and applies the current
  retention to every table listed above; archived rows land in
  `$DATA_DIR/archive/<table>.jsonl.gz`.

## LLM data handling

See §11 redaction details. Additional rules:

- The household's LLM settings page shows exactly which
  capabilities send data upstream, which fields are redacted, and a
  per-capability toggle to **disable upstream entirely** (turns the
  feature off).
- Instruction bodies sent upstream only when the capability requires
  grounding (default on). Toggle exists.
- Uploaded photos sent upstream for `expenses.autofill` only; never
  for `tasks.assist`.
- No PII is sent in telemetry. There is no product telemetry in v1.

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

## Items deferred but noted

- WebAuthn signed enterprise attestation inspection.
- OIDC as an alternative manager auth method.
- SOC 2 / HIPAA posture (out of scope for a household tool).
- End-to-end encryption of task content between managers and staff
  (we operate server-side; any E2EE would defeat the agent layer).
