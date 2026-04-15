# 03 — Authentication, sessions, tokens

## Principles

- **No passwords.** For anyone. Not for managers, not for employees.
- **Passkeys (WebAuthn)** are the only human credential.
- **Magic links** are only an enrollment mechanism — they register a
  passkey; they do not authenticate a session on their own.
- **Agents** use long-lived, revocable, scope-limited API tokens.
- The server never stores anything that can be replayed if the DB
  leaks: credentials are public keys, tokens are stored as argon2id
  hashes.
- Every enrollment, login, rotation, and revocation writes to the
  audit log (§02).

## Actors

- **Manager.** Human with elevated scope. All managers are peers — no
  hierarchy in v1, but the model allows it (§05).
- **Employee.** Human with scope limited to their own data plus the
  tasks and properties they are assigned to.
- **Agent.** Non-human. Identified by an API token; never by a session.
- **System.** The worker process itself, when generating scheduled
  tasks, sending digests, polling iCal. No token — identified by a
  reserved `actor_id = "00000000000000000000000000"` in the audit log.

## Enrollment flows

### Manager (initial)

1. First-boot wizard runs once when the DB has no `manager` rows. The
   CLI `miployees admin init --email owner@example.com` creates the
   household and emails the owner a **bootstrap magic link** valid for
   15 minutes.
2. Owner clicks the link, chooses a display name and timezone,
   registers a passkey on their current device.
3. System generates **break-glass recovery codes** (8 codes, 10 chars
   Crockford base32, shown once, stored argon2id-hashed in
   `break_glass_code`). Owner must confirm "I wrote them down" before
   proceeding. Each code is single-use: a successful code
   redemption generates exactly one magic link (15-min TTL) and marks
   the code row `used_at = now()`. The consumed code is inert even if
   the resulting magic link expires unused — the owner must consume
   another code to get a fresh link.

### Manager (additional)

- An existing manager invites another via
  `POST /api/v1/managers/invite` with `{ email, display_name }`.
- System emails a magic link (15 min TTL). On acceptance, recipient
  registers a passkey and receives their own set of break-glass codes.

### Employee

1. Manager creates an employee record with `{ display_name, email,
   role_ids[], property_ids[] }`.
2. Manager clicks "Send magic link" (or calls
   `POST /api/v1/employees/{id}/magic_link`). The system emails a
   link valid for 24 hours.
3. Employee clicks link on their phone, the page prompts them to
   register a passkey (platform authenticator preferred — Face ID,
   Touch ID, Android screen lock). They may add a second passkey for
   a backup device.
4. Employees do **not** receive break-glass codes; recovery is by
   manager re-issue of a magic link (see below).

### Additional passkeys

- Any logged-in user can add another passkey from their profile page.
  Up to 5 passkeys per user.
- Each passkey carries a user-editable `nickname` ("work phone",
  "wife's iPad").

### Re-enrollment side-effects

When a manager re-issues a magic link to a user ("Employee lost
phone" or "Manager lost device" paths below), accepting the link and
registering a fresh passkey:

1. Revokes **all existing passkeys** for that user (the new one is
   written after revocation in the same transaction).
2. Revokes **all active sessions** for that user; they must log in
   again on every previously-signed-in browser.
3. For managers: regenerates the break-glass code set (old codes
   invalidated).

All three events land in the audit log under `auth.reenroll`.

## Login

- `/login` renders a single button: **"Continue with passkey"**.
- WebAuthn conditional UI (`mediation: "conditional"`) is used so
  browsers that support it prompt silently from the username field;
  browsers that do not fall back to the button.
- Passkey credential ID discovers the user — we do not ask for email.
- Successful assertion issues a session cookie.

## Sessions

- Session cookie: `__Host-miployees_session`.
- Flags: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no `Domain`.
- Value: opaque random 192-bit token → hashed row in `sessions` table.
- Lifetime: 30 days for employees, 7 days for managers (configurable).
  Refreshed on each request after half its lifetime has elapsed.
- CSRF: HTMX requests carry a double-submit token
  (`miployees_csrf` cookie + `X-CSRF` header) for every non-GET. Same
  origin is enforced by `SameSite=Lax` for initial navigation.

## API tokens

### Creation

- A manager creates a token via the UI or
  `POST /api/v1/auth/tokens`:
  ```json
  {
    "name": "hermes-scheduler",
    "scopes": ["tasks:read", "tasks:write", "stays:read"],
    "expires_at": "2027-01-01T00:00:00Z",
    "note": "nightly scheduling agent"
  }
  ```
- Response shows the **plaintext token once**; never again.
- Token format: `mip_<key_id>_<secret>` where `key_id` is a public
  ULID and `secret` is 256 bits of base32. Only the argon2id hash of
  the secret is stored; the key_id is stored in the clear so that
  every request can be O(1) located.

### Scopes

Fine-grained, resource-scoped verbs. An agent should be issued the
narrowest set possible.

- `tasks:{read,write,complete}`
- `employees:{read,write}`
- `properties:{read,write}`
- `stays:{read,write}`
- `inventory:{read,write,adjust}`
- `time:{read,write}`
- `expenses:{read,write,approve}`
- `payroll:{read,run}`
- `instructions:{read,write}`
- `messaging:{read,write}`
- `llm:{read,call}` — `call` required to execute model calls chargeable
  to the household
- `admin:{impersonate,rotate,purge}` — rare; requires approval of
  another manager before first use (see §11 approval workflow)

`*:read` implied by `*:write`. `admin:*` implies nothing else — it is a
narrow escape hatch.

### Usage

`Authorization: Bearer mip_<key_id>_<secret>`

- 401 on absent or malformed token.
- 403 on insufficient scope (with `WWW-Authenticate: error="insufficient_scope"
  scope="tasks:write"`).

### Revocation and rotation

- Any manager can revoke any token. Revocation takes effect within 5
  seconds (token cache TTL).
- Tokens can be rotated in place: the old secret hash is kept alongside
  the new for a configurable overlap (default 1h), so long-running
  agents can reload without downtime.
- A **per-token audit log view** is available in the UI: every request,
  method, path, response status, and `audit_correlation_id` link.

### Guardrails

- Tokens cannot create tokens unless scope `admin:rotate` is granted.
- Tokens cannot accept their own `admin:*` approval (§11).
- Tokens default to 90 days TTL if `expires_at` is omitted. A
  household-level setting can raise the default to "never" but emits a
  noisy warning in the UI.
- IP allow-lists optional per token (CIDR, comma-separated). Violations
  log and 403.

## Recovery paths

| Situation                                  | Recovery path                                                  |
|--------------------------------------------|----------------------------------------------------------------|
| Employee lost phone                        | Any manager clicks "re-issue magic link" on their profile; current passkeys are revoked on registration. |
| Manager lost only device, has backup code  | Enter recovery code → magic link emailed → register passkey; one backup code is burnt. |
| Manager lost device + all backup codes, another manager exists | Any other manager can re-issue a magic link to their email. |
| Last manager locked out completely         | **Host-CLI recovery only in v1.** Stop service, run `miployees admin recover --email ...` on the host, which emits a one-time magic link to stdout. Operator must have shell access to the deployment host. Hosted / SaaS recovery flows (support escalation, out-of-band identity verification) are **out of scope for v1** — see §19. |
| Employee email address wrong / changed     | Manager updates email on their profile; next magic link goes to the new one. |

## Break-glass codes

```
break_glass_code
├── id                   ULID PK
├── household_id         ULID FK
├── manager_id           ULID FK
├── hash                 argon2id digest of the code
├── hash_params          argon2id parameters (for upgrade)
├── created_at           tstz
├── used_at              tstz?  (null until redeemed)
└── consumed_magic_link_id ULID?  populated on redemption
```

Redemption: the manager submits the plaintext code to
`POST /auth/magic/consume` with their email. On success the code's
`used_at` is set, a fresh `magic_link` is issued (15-min TTL), and its
id is stored in `consumed_magic_link_id`. A used code is inert even
if the resulting magic link expires unused.

## Magic link format

- URL: `https://<host>/auth/magic/{token}`
- `token` is an `itsdangerous` signed blob: `{ purpose, subject_id,
  jti, exp }` signed with the household's magic-link key.
- Single use (`jti` recorded on successful consumption).
- Open attempts after consumption or expiry show a polite re-request
  page, and rate-limit the offending IP (§15).

## WebAuthn specifics

- RP ID: configured hostname (e.g. `ops.example.com`).
- User verification: `required` (matches "passkey" semantics).
- Authenticator attachment: `platform` preferred, `cross-platform`
  allowed (YubiKey for managers).
- Resident keys (discoverable credentials): `preferred`.
- Attestation: `none` — we trust the browser's RP ID binding.
- Algorithms: ES256 (`-7`), RS256 (`-257`) for broader iOS/Android
  support.

## Privacy

- We store only: credential ID, public key, sign count, AAGUID,
  transport hints, nickname, last_used_at.
- We never store a device fingerprint beyond AAGUID (which only
  identifies the authenticator model).
- `last_used_at` is visible only to the owning user.
