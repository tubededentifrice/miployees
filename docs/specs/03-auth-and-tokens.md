# 03 — Authentication, sessions, tokens

## Principles

- **No passwords.** For anyone. Not for owners, not for workers.
- **Passkeys (WebAuthn)** are the only human credential.
- **Magic links** never authenticate a session on their own. They are
  a one-shot consent + ownership proof for four narrow purposes:
  (1) first-passkey enrollment, (2) accepting a grant invitation,
  (3) self-service lost-device recovery (registers a fresh passkey,
  revokes the old ones), and (4) verifying a self-service email
  change. Every purpose is stamped into the signed token and
  rejected if redeemed against the wrong endpoint.
- **Standalone agents** use long-lived, revocable, scope-limited API
  tokens. **Embedded agents** (§11) use delegated tokens that inherit
  the calling user's full permissions (see "Delegated tokens" below).
- The server never stores anything that can be replayed if the DB
  leaks: credentials are public keys, tokens are stored as argon2id
  hashes.
- Every enrollment, login, rotation, and revocation writes to the
  audit log (§02).

## Actors

- **User.** Every human is a `users` row (§02) with at least one
  `role_grants` row that places them on a surface somewhere. A
  user may hold a `manager` surface on one workspace, a `worker`
  surface in another, and a `client` surface on a single property
  in a third, simultaneously, plus membership in any number of
  permission groups (including `owners`) for governance. All
  actions by humans — regardless of surface — log as
  `actor_kind = 'user'`; the surface the action was taken from is
  captured in `actor_grant_role`, and whether the actor was an
  `owners` member at the time is captured in
  `actor_was_owner_member` (§02 audit_log).
- **Agent.** Non-human. Standalone agents are identified by a scoped
  API token; never by a session. Embedded agents (§11) use **delegated
  tokens** that act as the creating user — their `actor_kind` in audit
  is `user`, with `agent_label` and `agent_conversation_ref` set so
  the row is clearly flagged as agent-executed.
- **System.** The worker process itself, when generating scheduled
  tasks, sending digests, polling iCal. No token — identified by a
  reserved `actor_id = "00000000000000000000000000"` in the audit log.

**"Manager", "employee", "client"** are **grant roles**, not user
kinds. Enrollment, passkey management, session shape, magic-link
flow, and break-glass recovery are identical across them; the
differences are entirely in what the user sees and can do once
authenticated (see §05 action catalog, §02 `role_grants`, and
§02 `permission_group` / `permission_rule`).

## Enrollment flows

Enrollment is unified: the same REST endpoints, magic-link flow,
and passkey ritual enroll every user, regardless of which grants
they will hold. The only things that vary by grant_role are the
default magic-link TTL and whether break-glass codes are issued on
acceptance (see "Break-glass codes" below).

### First owner (self-hosted first boot)

This is the self-hosted bootstrap flow, used by operators running
`crewday` on their own infrastructure. The SaaS deployment at
`crew.day` uses the **Self-serve signup** flow below instead.

1. First-boot wizard runs once when the DB has no `users` rows. The
   CLI `crewday admin init --email owner@example.com --slug myhome`
   creates the workspace (with `created_via='admin_init'`,
   `verification_state='trusted'`) and emails that user a
   **bootstrap magic link** valid for 15 minutes. The wizard atomically inserts a `users` row, a
   single `role_grants` row with
   `(scope_kind='workspace', scope_id=<ws>, grant_role='manager')`
   (the manager surface), seeds the four system permission groups
   (`owners`, `managers`, `all_workers`, `all_clients`) on the new
   workspace, and inserts a `permission_group_member` row placing
   the user in `owners@<ws>` — the governance anchor required by
   the ≥1-owners-member invariant (§02).
2. User clicks the link, chooses a display name and timezone,
   registers a passkey on their current device.
3. System generates **break-glass recovery codes** (8 codes, 10 chars
   Crockford base32, shown once, stored argon2id-hashed in
   `break_glass_code`). The bootstrap user must confirm "I wrote
   them down" before proceeding. Each code is single-use: a
   successful code redemption generates exactly one magic link
   (15-min TTL) and marks the code row `used_at = now()`. The
   consumed code is inert even if the resulting magic link expires
   unused — the user must consume another code to get a fresh
   link.

### Self-serve signup

Self-serve signup is a first-class, always-available flow on every
deployment (§00 G12). The managed SaaS at `crew.day` runs it
with `settings.signup_enabled = true` so any visitor can provision
a workspace; a home-network self-host typically runs it off
(`settings.signup_enabled = false`) so nobody on the LAN can
create a workspace without the operator's say-so. The flag is an
operator-settable deployment setting (§01 "Capability registry"),
not a deploy-mode gate — flipping it does not take the server
offline and does not change any other codepath.

When disabled, `POST /api/v1/signup/start` returns `404` and the
`/signup` SPA route renders a "Signups are closed on this
deployment — ask your admin" page; all other flows are
unaffected.

The flow lives entirely at the bare host (no `/w/<slug>/` prefix)
because no workspace exists yet:

1. **Email entry.** Visitor lands on `/signup`, submits an email.
   Request hits `POST /signup/start` with `{ email, desired_slug }`.
   Per-IP and per-email rate limits (§15 "Self-serve abuse
   mitigations") apply; disposable-domain blocklist rejects
   throwaway providers. `desired_slug` is validated against the
   §02 regex and the reserved-slug guard below, then checked
   against live `workspaces` rows; `409 slug_taken` returns a
   suggested alternative. The server stores a `signup_attempt`
   row keyed by `(email, desired_slug)` with a 15-minute TTL and
   emails a magic link (`/signup/verify?token=...`).

   **Reserved slugs.** The following labels are permanently
   reserved — `POST /signup/start` rejects them with
   `409 slug_reserved`, and the admin-init path (§ "First-owner
   bootstrap") refuses them too:

   ```
   admin, api, app, assets, auth, demo, docs, events, guest,
   healthz, login, logout, public, readyz, recover, signup,
   static, status, support, version, w, webhooks, ws, www
   ```

   This list is the operational superset of §02's blocklist and
   MUST stay in sync with the reverse-proxy routing table (§01,
   §16) — any future route reserved at the bare host gets an
   entry here first. The list lives alongside the §02 regex as a
   constant in `app.tenancy.slug` and is loaded by both signup
   and admin-init.

   **Homoglyph guard.** Reject any `desired_slug` whose
   ASCII-folded, Punycode-normalized, digit-substituted form
   (`0 → o`, `1 → l`) collides with an existing active slug.
   Example: `rnicasa` is rejected when `micasa` is taken (rn vs
   m); `0wner` is rejected when `owner` is taken. Error:
   `409 slug_homoglyph_collision` with the colliding existing
   slug in the error body so the signup UI can explain the
   rejection. The fold is purely defensive — the regex in §02
   already restricts the character set, but the fold catches
   typographic look-alikes registered before a similar slug gets
   trademarked.

   **Grace period.** On workspace archive
   (`verification_state='archived'`) or hard delete, the slug is
   copied to a `slug_reservation` row (§02) with a **30-day**
   hold before any other signup may claim it. Attempts inside
   the window return `409 slug_in_grace_period` with the
   `reserved_until` value in the error body. The
   `slug_reservation` row carries `previous_workspace_id` (for
   audit), `reserved_until` (the reservation's expiry), and
   `reason` (`archived | hard_deleted | system_reserved |
   homoglyph_guard`) — see §02 for the full schema. The signup
   worker prunes expired rows during `signup_gc`. A manager who
   archives a workspace by mistake can recover the slug within
   the window via `crewday admin workspace unarchive`, which
   clears the reservation atomically.
2. **Magic-link verification.** Visitor clicks the link. `GET
   /signup/verify` redeems the token. The server atomically, in a
   single transaction:
   - inserts the `workspaces` row
     (`created_via='self_serve'`, `verification_state='email_verified'`,
     `plan='free'`, `quota_json` copied from the free-tier caps,
     `signup_ip` set to the `POST /signup/start` request's source
     IP — see §15 "Per-IP aggregate LLM spend cap");
   - inserts the `users` row, a `role_grants` row with
     `(scope_kind='workspace', scope_id=<ws>, grant_role='manager')`,
     the four system permission groups on the new workspace, and
     the `permission_group_member` row placing the user in
     `owners@<ws>`;
   - writes `audit.signup.completed` with `actor_kind='system'`
     (pre-session) and `ip_hash` for abuse tracing.
3. **Passkey enrollment + break-glass codes.** Same ritual as the
   self-hosted first-owner flow: display name + timezone, register
   a passkey, receive break-glass codes, confirm "I wrote them
   down". The signup session is issued only after passkey
   registration succeeds — a stranded magic-link click followed by
   an abandoned passkey ceremony leaves a row in `workspaces` with
   no user and no session; the `signup_gc` worker prunes these
   after 1 hour (see §15).
4. **Ready.** Browser is redirected to
   `https://crew.day/w/<slug>/today`. The workspace is now
   addressable; session cookies (`__Host-crewday_sess`) are
   set on the bare host and used for every workspace the user
   later joins.

**Tight initial caps.** Until the workspace reaches
`verification_state='human_verified'` (defined in §02), the LLM
budget cap is **10% of the free-tier cap** and the upload quota is
**25 MB**. Both lift automatically once the manager completes the
human-verification trigger (one property created + one user
invited + one task created). See §15 for the full cap table and
abuse-response playbook.

**Throttle on repeat provisioning.** The same email may provision
at most **3 workspaces** lifetime on the SaaS deployment
(enforced on `POST /signup/start`). The same IP may start at most
**5 signups per hour**. Limits are documented in §15 and are
configurable by the SaaS operator; self-host operators override
via env vars.

### Additional users (invite → click-to-accept)

Invitations are **click-to-accept**, uniformly — whether the
invitee already has a `users` row or not. The recipient always
sees an Acceptance card listing the grants being added before
they take effect; grants never attach silently.

- A user who passes the `users.invite` action check on the target
  scope (owners and managers by default — see §05 action catalog)
  invites another via `POST /api/v1/users/invite` with
  `{ email, display_name,
     grants: [ {scope_kind, scope_id, grant_role, binding_org_id?}, ... ],
     permission_group_memberships?: [ {group_id}, ... ],
     work_engagement?: {workspace_id, engagement_kind, ...},
     user_work_roles?: [ {workspace_id, work_role_id}, ... ] }`.
  One call creates (or re-uses, if `email` matches an existing
  row) the `users` row and inserts a pending `invite` record
  carrying the requested grants, permission-group memberships,
  work engagement, and work-role mappings. Nothing on the invited
  scope is active yet. The requested rows live alongside the
  invite as `pending = true` and are activated atomically on
  acceptance. Inviting someone directly into `owners@<scope>`
  requires the inviter to pass the root-only
  `groups.manage_owners_membership` action check for that scope.
- System emails a magic link of purpose `accept` that lands on
  `/w/<slug>/accept/<token>` (24 h TTL across all surfaces;
  single-use; `jti` recorded). The same endpoint handles both
  cases:
  - **New user**: the redemption is followed inline by the
    passkey enrollment ceremony (display name confirmation,
    timezone, `passkey/register/finish`). On success the
    pending grants are activated, and — for invitees into a
    `manager` surface grant or any `owners` permission group —
    a set of break-glass codes is generated (same ritual as the
    self-serve signup flow).
  - **Existing user**: the redemption prompts a passkey sign-in
    if no active session is present, then renders the Acceptance
    card. The card lists the exact grants, group memberships, and
    work-role rows that will activate. On **Accept**, the pending
    rows activate in a single transaction; on **Dismiss**, they
    are left pending until the invite TTL lapses, at which point
    the nightly `signup_gc` worker prunes them. No existing
    passkey is re-registered; no break-glass regeneration.
- Two audit events distinguish the two outcomes: `user.enrolled`
  on first-passkey completion, `user.grant_accepted` on the
  existing-user Accept. Both carry the `invite.id`, the list of
  activated grant ids, and the `actor_grant_role` of the
  inviter.
- If the invite's `email` is changed by the recipient on the
  `/me` page before acceptance (self-service email change;
  below), the pending invite rides along on `user_id`, not on the
  old email — no re-send is required.

### Additional passkeys

- Any logged-in user can add another passkey from their profile page.
  Up to 5 passkeys per user.
- Each passkey carries a user-editable `nickname` ("work phone",
  "wife's iPad").
- Users may **revoke** their own passkeys from the same profile page
  (`DELETE /w/<slug>/api/v1/auth/passkey/{credential_id}`, §12). The
  server refuses to revoke the user's last remaining credential — a
  credential-less account would be forced through recovery to sign
  in again, so the SPA either steers the user to enrol another
  passkey first or (deliberately) through §"Self-service lost-device
  recovery" as the break-glass. A credential id that belongs to
  another user is indistinguishable from an unknown id and both
  collapse to `404 passkey_not_found` so the credential-id space is
  not an enumeration oracle; admin-initiated revocation rides on
  §"Owner-initiated worker passkey reset" instead.
- Every successful revoke invalidates **every active session** for
  that user — including the caller's own session — in the same UoW
  as the delete, via the `invalidate_for_user` seam described in §15
  "Shared-origin XSS containment". The invalidation row carries
  `cause = "passkey_revoked"` (catalogued in §15 "Session-
  invalidation causes"). Registering a new passkey is also a
  credential-population change and invalidates every session for
  the user with `cause = "passkey_registered"` — the router seam
  doesn't know the caller's own session PK, so the SPA re-auths
  after the ceremony. Forensic rows survive both paths so operators
  can join sign-in → session → subsequent activity after the fact.

### Re-enrollment side-effects

Re-enrollment happens whenever a fresh passkey is registered
through a magic link of purpose `recover` — whether the magic
link was minted by a manager via `users.reissue_magic_link`, by
a self-service recovery request (§"Self-service lost-device
recovery" below), or by consuming a break-glass code. The
ceremony's final `passkey/register/finish` call, in a
single transaction:

1. Revokes **all existing passkeys** for that user (the new one is
   written after revocation in the same transaction).
2. Revokes **all active sessions** for that user; they must log in
   again on every previously-signed-in browser.
3. For users who hold a `manager` surface grant anywhere **or**
   who are members of any `owners` permission group, regenerates
   the break-glass code set (old codes invalidated). Users who
   hold only `worker` / `client` / `guest` surface grants and are
   not `owners` members anywhere have no code set to regenerate.

All three events land in the audit log under `auth.reenroll`
with a `trigger` column of
`manager_reissue | self_service | break_glass`.

### Worker recovery flow — three layered paths

Workers frequently lose phones, change devices, or arrive without
a working email address. v1 lands three layered paths so the
human surface always has an exit and no single compromised actor
can unilaterally impersonate a worker:

1. **Email magic-link (default)** — see "Self-service lost-device
   recovery" immediately below.
2. **Owner-initiated reset (safety net)** — see "Owner-initiated
   worker passkey reset" below.
3. **Printable welcome packet** — see "Printable welcome packet"
   below; minted at invite time, carried on paper.

Each path independently satisfies the enrollment-magic-link
requirement; they differ in *who triggers it* and *who is copied
on the notification*, not in *what lands on the device*.

### Self-service lost-device recovery

Users who have lost access to every device with a registered
passkey can re-enroll from any browser without waiting on a
manager. The flow is an **enrollment magic link**, not an
authenticated session — §"Principles" still holds.

**Entry point.** `/recover` (bare host). Form asks for the
account email. Managers and owners-group members see a second
field: **"Break-glass code"**. Workers, clients, and guests see
only the email field. The UI copy makes clear that the code
field is required for owners and managers.

**Request.** `POST /api/v1/auth/recover/start` with
`{ email, break_glass_code? }`. The server:

1. Applies per-email and per-IP rate limits (§15 self-serve abuse
   mitigations — same family used on `signup/start`).
2. Looks up `users.email` case-insensitively. If no match, logs
   `auth.recover.miss` with `ip_hash` and `email_hash`. **Always
   returns 200** with the generic body
   `{ "status": "sent_if_exists" }` regardless of the lookup
   outcome — the response does not reveal which emails map to a
   user, nor which users require step-up.
3. If the user holds a `manager` surface grant anywhere **or** is
   a member of any `owners` permission group — the **step-up
   population** — the request is only honoured when
   `break_glass_code` is present and matches an unused
   `break_glass_code` row for that user. A matching code is burnt
   (`used_at = now()`) and a magic link of purpose `recover`
   (15-min TTL, single-use) is mailed. Missing or invalid code
   logs `auth.recover.stepup_missing` or
   `auth.recover.stepup_invalid` and sends nothing. A burnt code
   is inert even if the resulting magic link expires unused —
   the user consumes another code to retry.
4. For the non-step-up population (workers, clients, guests with
   no manager or owners membership anywhere), the code field is
   ignored and never burnt; a recover-purpose magic link is
   mailed directly.
5. If **any** workspace the target user holds a non-archived
   grant in has `auth.self_service_recovery_enabled = false`
   (§"Workspace kill-switch" below; *most-restrictive-wins*), no
   email is sent and `auth.recover.disabled_by_workspace` is
   logged. The 202 body is unchanged. The user falls back to
   manager-mediated recovery (existing
   `users.reissue_magic_link` path).

**Redemption.** The magic link lands on
`/recover/enroll?token=…`, which:

1. Verifies the token signature, purpose (`recover`), expiry, and
   single-use `jti`.
2. Walks the user through the passkey-registration ceremony
   (display name confirmation, timezone if missing, a WebAuthn
   `finish_registration`).
3. Applies the "Re-enrollment side-effects" above in the same
   transaction as writing the new passkey.

**Manager-initiated path unchanged.** A user who passes
`users.reissue_magic_link` on a shared scope can still click
"re-issue magic link" on another user's profile
(`POST /api/v1/users/{id}/magic_link`). That path skips the
workspace kill-switch entirely — it is the fallback for
deployments that disable self-service.

### Owner-initiated worker passkey reset

Any workspace owner MAY tap **"Reset worker passkey"** on a
worker's profile (`POST /api/v1/users/{id}/reset_passkey`). This
differs from `users.reissue_magic_link` in one important way:
the server mails the enrollment magic link to **both** the
worker's email **and** the owner's email. The email body sent to
the owner is an **audit notification copy**, not a second valid
token — it carries a rendered summary of the action ("You reset
the passkey for Marie L. on <timestamp>; a magic link has been
mailed to her at m***@example.com") and the link in it is **not
claimable**: clicking it lands on `/recover/notice` with a
human-readable "this is your copy; the worker clicks the link in
her own email" page.

Concretely:

- The worker's email carries the real magic link
  (`/recover/enroll?token=…`); consuming it enrols a fresh
  passkey under the worker's account.
- The owner's email carries a
  *non-consumable* copy with the worker's email address masked
  (`m***@example.com`) plus a prominent "Not you?" link that
  reports the action for review.
- If the owner forwards their own email's non-consumable link to
  the worker, the worker sees the notice page — not an enrolment
  ceremony — and both parties can see that the action started.
- A compromised owner account therefore cannot unilaterally
  impersonate a worker: the enrolment still requires the
  worker's mailbox.

Error codes:

- `reset_requires_worker_consent` — the owner tried to enrol a
  passkey under the worker's account using the notification-copy
  link.

### Printable welcome packet

At invite time, `POST /api/v1/users/invite` (§12) returns a
one-page PDF under `Content-Type: application/pdf` containing:

- a **passkey-enrolment QR code** pointing at a tokenised enrol
  URL (the same token used in the email invite, so the PDF and
  the email are equivalent on first use);
- the **workspace identifier** and the **worker's display
  name**;
- a **"recover from any device" URL**
  (`https://<host>/recover?u=<worker_user_id_public>`) so a
  worker without the QR-capable device can still find the right
  starting page;
- the **owner's phone number** (if set in workspace contact
  settings) for offline contact;
- plain-language copy: *"If you lose your phone, visit this link
  from any device and we'll email you a new setup code. If you
  can't get email, ask the person who invited you."*

The manager UI offers a **"Download PDF"** button next to every
invite in the outbox and a **"Print packets"** bulk action in
the Employees list that renders one page per selected user. The
PDF is generated server-side from the same template the welcome
email uses, and it carries no cryptographic material beyond the
same invite token already in the email; losing the packet is no
worse than losing a single invite email and is mitigated the
same way (owner revokes the invite via
`DELETE /api/v1/users/invite/{id}`).

### Workspace kill-switch

A workspace setting
`auth.self_service_recovery_enabled` (bool, default `true`,
override scope `W`, registered in §02 "Settings cascade" catalog)
gates the self-service path **for members of that workspace only**. Because
identity is global and a user may hold grants in multiple
workspaces, the server evaluates the flag as
*most-restrictive-wins*: if **any** workspace the user holds a
non-archived grant in has the flag `false`, self-service recovery
is refused for that user (step 5 above). Managers may still
re-issue a magic link manually; break-glass codes still redeem
through `POST /auth/magic/consume`; and the host-CLI recovery
remains available to the deployment operator (§"Recovery paths").

There is no per-user opt-out: a locked-out user cannot flip a
personal setting. Users who want individual protection should ask
an owner to raise the bar at workspace scope.

### Self-service email change

`users.email` is the identity anchor for every magic-link flow
above. A user can change their own address from `/me` without
manager intervention; the change is gated on proving control of
the new mailbox.

**Request.** `POST /api/v1/me/email/change_request` with
`{ new_email }`, from a passkey session only (no PAT, no
delegated token — `me.profile:write` does **not** unlock the
email field; the field is self-service via the session cookie
only). The server:

1. Validates syntax and canonicalises (trim + lowercase).
2. Rejects with 409 `error = "email_in_use"` if another
   non-archived `users` row already holds it (case-insensitive).
3. Rejects with 409 `error = "recent_reenrollment"` if the
   caller's passkey was registered less than **15 minutes** ago
   — this bounds the window in which an attacker who just hijacked
   the account via a compromised magic link could pivot to a new
   mailbox.
4. Issues a magic link of purpose `email_change` to the **new**
   address only. The token payload carries `user_id` and
   `pending_new_email`; 15-min TTL; single-use.
5. Sends an informational, link-free notice to the **old** address
   ("Someone requested changing the email on your crew.day account
   to <masked-new>. If this wasn't you, contact your manager.")
   with the caller's IP prefix for provenance.
6. Writes `auth.email_change_requested` with `actor_id`,
   `old_email_hash`, `new_email_hash`, `ip_hash`.

**Confirmation.** The recipient clicks the link on the new
address, which calls
`POST /api/v1/auth/email/verify { token }`. The server:

1. Validates the signature, purpose (`email_change`), expiry, and
   single-use `jti`.
2. Requires an active passkey session for the same
   `user_id` — opening the link on a signed-out browser prompts
   a passkey sign-in (on any device that still has a passkey)
   before the swap. An attacker with mailbox access alone cannot
   complete the swap.
3. Re-checks uniqueness and swaps `users.email` atomically with
   the `jti` consumption.
4. Writes `auth.email_changed` with the old/new hashes; sends a
   notice to **both** addresses ("Your email was changed to
   <masked-new>").

**Revert window.** The notice to the old address includes a link
to `POST /api/v1/auth/email/revert { token }` signed with a
72-hour TTL. Redemption reverts `users.email` to the old value
and logs `auth.email_change_reverted`. The revert link is the
only flow that consumes a magic link against the **old** address
after the swap — it is not an authentication primitive.

## Login

- `/login` renders a single button: **"Continue with passkey"**.
- WebAuthn conditional UI (`mediation: "conditional"`) is used so
  browsers that support it prompt silently from the username field;
  browsers that do not fall back to the button.
- Passkey credential ID discovers the user — we do not ask for email.
- Successful assertion issues a session cookie.

## Sessions

- Session cookie: `__Host-crewday_session`.
- Flags: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no `Domain`.
- Value: opaque random 192-bit token → hashed row in `sessions` table.
- Lifetime: 7 days for users who hold a `manager` surface grant
  on any scope **or** who are members of any `owners` permission
  group; 30 days for everyone else
  (configurable). Recomputed on login, not mid-session — a user who
  gains a manager grant mid-session keeps the longer lifetime until
  their next login. Refreshed on each request after half its
  lifetime has elapsed.
- CSRF: Authenticated SPA requests carry a double-submit token
  (`crewday_csrf` cookie + `X-CSRF` header) for every non-GET. Same
  origin is enforced by `SameSite=Lax` for initial navigation.

## API tokens

### Creation

- Any user who passes the `api_tokens.manage` action check on the
  workspace (owners and managers by default, §05) creates a scoped
  workspace token via the UI at `/tokens` or
  `POST /w/<slug>/api/v1/auth/tokens`:
  ```json
  {
    "label": "hermes-scheduler",
    "scopes": {"tasks:read": true, "tasks:write": true, "stays:read": true},
    "expires_at_days": 365
  }
  ```
  `label` is a human-readable identifier (1–160 chars) shown on the
  `/tokens` admin list and stamped into audit rows as `agent_label`.
  `expires_at_days` is a positive integer count of days (1–3650,
  defaulting to 90 when omitted); the server computes the absolute
  `expires_at` timestamp from it at mint time and returns the ISO
  value on the response for the client's reference.
- `scopes` is a flat `{"<action_key>": true}` dict — the same shape
  the `api_token.scope_json` column stores, so the router holds no
  list-to-dict coercion. The key is an action string from the scope
  catalog below; the value is truthy (v1 uses `true`; reserved for a
  future per-scope constraint payload). Delegated tokens send
  `scopes: {}` — see "Delegated tokens" below.
- Response shows the **plaintext token once**; never again.
- Token format: `mip_<key_id>_<secret>` where `key_id` is a public
  ULID and `secret` is 256 bits of base32. Only the argon2id hash of
  the secret is stored; the key_id is stored in the clear so that
  every request can be O(1) located.

### Delegated tokens

A **delegated token** is created by a logged-in user and inherits
**all permissions** of that user for as long as the user's account is
active and unarchived. This is the mechanism the embedded chat
agents (§11) use to act on behalf of their user.

```json
POST /api/v1/auth/tokens
{
  "label": "chat-agent",
  "delegate": true,
  "expires_at_days": 30,
  "scopes": {}
}
```

Key properties:

- `delegate_for_user_id`: ULID of the creating user — set from the
  session. Not caller-supplied.
- `scopes`: **empty**. Permission checks resolve against the
  delegating user's `role_grants` (and the work-role narrowing rules
  in §05), not against explicit token scopes. If the user's grants
  change (new grant added, existing grant revoked, property
  reassignment), the delegated token's effective permissions change
  immediately.
- If the delegating user is archived, globally deactivated, or loses
  every non-revoked grant, requests with the token return `401`
  with a clear message.
- A delegated token can only be created by a **passkey session** — it
  cannot be created by another token (no transitive delegation).
- Default TTL: **30 days** (shorter than the 90-day default for scoped
  tokens). A workspace-level setting can raise it, with the same
  noisy warning as for scoped tokens.
- Revocation: the delegating user can revoke their own delegated
  tokens; any user with the `users.revoke_grant` grant-capability
  (owners and managers by default) in any scope that the delegating
  user is active in can revoke that user's delegated tokens.
- Every write made through a delegated token is filtered through
  the delegating user's **agent approval mode** (§11 "Per-user
  agent approval mode"): `bypass` never pauses, `auto` pauses on
  routes that carry an `x-agent-confirm` annotation, `strict`
  pauses on every mutation. Workspace policy actions still land
  on `/approvals` regardless of mode. The user changes their own
  mode on their profile and no other user can change it for them.

**`api_token` columns for delegation:**

| column                | type   | notes                                     |
|-----------------------|--------|-------------------------------------------|
| `delegate_for_user_id` | ULID? | nullable; references `users.id`           |

When null the token is a classic scoped token (backward
compatible). When set, it is a delegated token; the
`actor_kind` in audit for requests using the token is `user`, with
`actor_id = delegate_for_user_id`, `agent_label = api_token.label`,
and the optional `agent_conversation_ref` header propagated in.

### Personal access tokens

A **personal access token (PAT)** is a scoped token minted by a
logged-in user **for themselves**, limited to the `me:*` scope
family, so any authenticated worker or client can write a small
script against their own data without a manager provisioning a
token for them. The canonical use case: a maid writing a one-file
script that prints today's tasks on her home printer.

```json
POST /api/v1/me/tokens
{
  "label": "kitchen-printer",
  "scopes": {"me.tasks:read": true, "me.bookings:read": true},
  "expires_at_days": 90
}
```

Key properties:

- `subject_user_id`: ULID of the creating user — set from the
  session, not caller-supplied. Identical role to
  `delegate_for_user_id` on delegated tokens but semantically
  distinct: a PAT can only read/write the subject's own rows (the
  `me:*` filter is applied at query time regardless of scope
  string), while a delegated token inherits *all* the subject's
  `role_grants`.
- `scopes`: **must** be drawn from the `me:*` family. Mixing
  `me:*` with workspace scopes on the same token is a 422
  `error = "me_scope_conflict"`. An empty scope list is a 422
  `error = "scopes_required"` (unlike delegated tokens, which
  require empty).
- A PAT can only be created by a **passkey session** — no
  transitive creation from another token.
- Default TTL: **90 days**. The workspace cap ("never" with noisy
  warning, see Guardrails) applies to PATs too, but the user can
  always override their own PAT to a shorter expiry — never a
  longer one than the workspace ceiling.
- Every user may create a PAT regardless of `grant_role`; the
  right to do so is an identity-scoped self-service verb anchored
  on the authenticated `users` row (§05 "Identity-scoped
  actions"), not an action-catalog entry.
- If the subject user is archived, globally deactivated, or loses
  every non-revoked grant in every workspace, PAT requests return
  `401` with a clear message. Reinstating the user reinstates
  their PATs only if they survived archive (spec is
  archive-preserves-rows; `users.archived_at` is set, the token
  stays but returns 401 until the archive flag clears).
- A PAT scoped to a workspace the user is no longer a member of
  returns `404 workspace_out_of_scope` — matching the behaviour
  of a scoped standalone token used against the wrong workspace.
- **Per-user cap: 5 PATs**, same shape as the 5-passkey cap.
  Creating the 6th returns 422 `error = "too_many_personal_tokens"`
  and asks the user to revoke one. This cap is separate from the
  workspace-wide cap below.
- PATs are visible and revocable by the subject user on the
  "Personal access tokens" panel on `/me` (§14). They are **not**
  listed on the
  workspace-wide `/tokens` admin page — a manager does not need
  to audit every worker's printer script. Workspace owners who
  need a kill-switch use `users.archive` (§05) or revoke the
  user's session + passkeys via `users.reissue_magic_link`, both
  of which cascade to that user's PATs.
- Every write made through a PAT is audited as `actor_kind = 'user'`,
  `actor_id = subject_user_id`, `agent_label = api_token.label`,
  plus `api_token_kind = 'personal'` so the row is filterable from
  a workspace PAT or a delegated token.

**`api_token` columns for subject narrowing:**

| column              | type   | notes                                     |
|---------------------|--------|-------------------------------------------|
| `subject_user_id`   | ULID?  | nullable; references `users.id`. Set only on personal tokens. Mutually exclusive with `delegate_for_user_id`. |
| `kind`              | text   | `'scoped' \| 'delegated' \| 'personal'`. Derived at insert time from which id columns are set; persisted so the revocation and listing queries stay O(1). |

### Scopes

Fine-grained, resource-scoped verbs. A standalone agent should be
issued the narrowest set possible. **Delegated tokens ignore scopes
entirely** — permissions are resolved from the delegating user's
access.

- `tasks:{read,write,complete}`
- `users:{read,write}`            (identity, grants, engagements)
- `properties:{read,write}`
- `stays:{read,write}`
- `inventory:{read,write,adjust}`
- `time:{read,write}`
- `expenses:{read,write,approve}`
- `payroll:{read,run}`
- `instructions:{read,write}`
- `messaging:{read,write}`
- `llm:{read,call}` — `call` required to execute model calls chargeable
  to the workspace
- `admin:{impersonate,rotate,purge}` — rare; requires approval of
  another `owners`-group member before first use (see §11 approval
  workflow)

A separate **`me:*`** scope family is reserved for
**personal access tokens** (next section) and may not be mixed with
workspace scopes on the same token:

- `me.tasks:{read}` — tasks assigned to the token's subject, plus
  unassigned tasks on properties in scope matching their
  `user_work_role`.
- `me.bookings:{read}` — the subject's own bookings and payslips.
- `me.expenses:{read,write}` — read own expense claims; write creates
  or edits drafts scoped to the subject. Never `expenses:approve`.
- `me.profile:{read,write}` — the subject's `users` row, limited to
  the fields the worker surface already lets them self-update
  (display name, avatar, timezone, emergency contact, language).

`*:read` implied by `*:write`. `admin:*` implies nothing else — it is a
narrow escape hatch. `me:*` implies nothing outside `me:*` — the
subject narrowing is enforced at the row level regardless of which
`me:*` verb the caller asked for.

### Usage

`Authorization: Bearer mip_<key_id>_<secret>`

- 401 on absent or malformed token.
- 403 on insufficient scope (with `WWW-Authenticate: error="insufficient_scope"
  scope="tasks:write"`).

### Revocation and rotation

- Any user who passes the `api_tokens.manage` action check (owners
  and managers by default) in the token's home workspace can revoke
  any scoped or delegated token in that workspace; scoped tokens and
  their own delegated tokens are always revocable by the creator.
  **Personal access tokens** are revocable only by their subject
  user or via a cascade (`users.archive`, `users.reissue_magic_link`)
  — a manager cannot revoke a worker's PAT directly from `/tokens`.
  Revocation takes effect within 5 seconds (token cache TTL).
- Tokens can be rotated in place: the old secret hash is kept alongside
  the new for a configurable overlap (default 1h), so long-running
  agents can reload without downtime.
- A **per-token audit log view** is available in the UI (inline on
  `/tokens` for workspace tokens; inline on `/me` for PATs): every
  request with its method, path, response status, IP prefix
  (truncated to `/24` for IPv4, `/64` for IPv6 per §15 PII-minimisation),
  `user_agent`, `audit_correlation_id` link, and timestamp. The list
  page shows `last_used_at` and the last-used IP prefix so managers
  can spot dormant tokens.

### Observability fields on `api_token`

| column              | type   | notes                                         |
|---------------------|--------|-----------------------------------------------|
| `last_used_at`      | tstz?  | Updated best-effort per request (coalesced to ≤1 write/minute per token to bound write amp). |
| `last_used_ip_hash` | text?  | argon2id hash of the last-used IP prefix; the prefix itself is kept in the per-request audit rows, not on the token row, so the token row never leaks PII. |
| `last_used_path`    | text?  | path of the most recent request, truncated to 256 chars; useful to spot a rotated-away token still pinging an old endpoint. |

### Guardrails

- Tokens cannot create tokens unless scope `admin:rotate` is granted.
  A personal access token can never create any token (no `admin:*`
  scope is selectable on a PAT), so scripting an exfiltration chain
  through a leaked PAT is not possible.
- Tokens cannot accept their own `admin:*` approval (§11).
- Scoped tokens default to 90 days TTL if `expires_at_days` is
  omitted; delegated tokens default to 30 days; personal access
  tokens default to 90 days. A workspace-level setting can raise any
  of them to "never" but emits a noisy warning in the UI.
- Delegated tokens cannot create other delegated tokens (no transitive
  delegation). A delegated token cannot outlive its delegating user's
  account — archiving the user effectively revokes all their delegated
  tokens. Archiving the user likewise cascades to their personal
  access tokens.
- **Workspace cap: 50 live scoped + delegated tokens per workspace.**
  Creating the 51st returns 422 `error = "too_many_workspace_tokens"`
  asking the user to revoke one. Personal access tokens do not count
  against the workspace cap — they are capped at **5 per user** (see
  "Personal access tokens" above).
- IP allow-lists optional per token (CIDR, comma-separated). Violations
  log and 403.

## Recovery paths

| Situation                                  | Recovery path                                                  |
|--------------------------------------------|----------------------------------------------------------------|
| Worker/client/guest lost every device      | **Self-service** via `/recover` — enter email, receive a magic link, register a fresh passkey. No break-glass code required. Available unless **any** workspace the user holds a non-archived grant in has `auth.self_service_recovery_enabled = false` (*most-restrictive-wins*, §"Workspace kill-switch"); managers on deployments that disable it use `users.reissue_magic_link` as before. |
| Manager or owners-member lost every device | **Self-service with step-up** via `/recover` — enter email **and** an unused break-glass code. The code is burnt on request; the magic link enrolls a fresh passkey and regenerates the code set. |
| Manager or owners-member lost every device + all break-glass codes, another owners-group member exists on a shared scope | Peer clicks `users.reissue_magic_link` on the user's profile. |
| Last owners-group member locked out completely | **Host-CLI recovery only in v1.** Stop service, run `crewday admin recover --email ...` on the host, which emits a one-time magic link to stdout. Operator must have shell access to the deployment host. Hosted / SaaS recovery flows (support escalation, out-of-band identity verification) are **out of scope for v1** — see §19. |
| Email address wrong (manager-initiated)    | A user who passes `users.edit_profile_other` on a shared scope updates email on the user's profile; used for account-admin fixes and for users who cannot reach `/me`. Since email is globally unique (§02), the change fails if another `users` row already holds that address. No verification email is sent on the new address — the manager vouches for the change. Audited as `user.email_changed` with `trigger = manager`. |
| User changes their own email               | **Self-service** via `/me` → "Email" panel, verified by a magic link sent to the new address; see §"Self-service email change" above. |

## Break-glass codes

```
break_glass_code
├── id                   ULID PK
├── workspace_id         ULID FK
├── user_id              ULID FK
├── hash                 argon2id digest of the code
├── hash_params          argon2id parameters (for upgrade)
├── created_at           tstz
├── used_at              tstz?  (null until redeemed)
└── consumed_magic_link_id ULID?  populated on redemption
```

Redemption: the user (whose codes were issued because they hold a
`manager` surface grant or `owners`-group membership) submits the
plaintext code to
`POST /auth/magic/consume` with their email. On success the code's
`used_at` is set, a fresh `magic_link` is issued (15-min TTL), and its
id is stored in `consumed_magic_link_id`. A used code is inert even
if the resulting magic link expires unused.

**Redemption rate limit.** Break-glass redemption is capped at
**3 failed attempts per user per 1-hour rolling window**. On the
3rd failure, redemption locks for **24 hours** for that user
(all subsequent `POST /auth/magic/consume` calls keyed to this
user return `429 break_glass_locked_out`, regardless of source IP
or client). The user and every workspace owner on any shared
scope receive an `audit.break_glass.locked_out` notification
through §10 messaging. Successful redemption burns the code
immediately (no replay) and resets the failure counter.

**Re-minting.** Code consumption does **not** automatically
regenerate the set (re-enrolment does, per "Re-enrollment
side-effects" above — that path invalidates every credential,
including the code set, in a single transaction). After a user
simply **uses a code** outside a re-enrolment (e.g. to pass a
step-up challenge that didn't trigger a full passkey rotation),
they MUST visit `/me/security` (§14) and re-mint the set by
hand. The **old codes remain valid** until explicitly replaced,
so the user is never locked out of recovery during the re-mint
step. The UI surfaces an amber banner on the home page for any
user whose unused-code count drops below 3 until they re-mint.

## Magic link format

- URL: `https://<host>/auth/magic/{token}`
- `token` is an `itsdangerous` signed blob: `{ purpose, subject_id,
  jti, exp }` signed with the workspace's magic-link key.
- Single use (`jti` recorded on successful consumption).
- Open attempts after consumption or expiry show a polite re-request
  page, and rate-limit the offending IP (§15).

## WebAuthn specifics

- RP ID: configured hostname (e.g. `ops.example.com`).
- User verification: `required` (matches "passkey" semantics).
- Authenticator attachment: `platform` preferred, `cross-platform`
  allowed (YubiKey for owners/managers).
- Resident keys (discoverable credentials): `preferred`.
- Attestation: `none` — we trust the browser's RP ID binding.
- Algorithms: ES256 (`-7`), RS256 (`-257`) for broader iOS/Android
  support.
- **Challenge rows are single-use, including on failure.** A
  `webauthn_challenge` row is deleted the moment its matching
  `/finish` HTTP call lands — whether the domain service accepted
  the assertion (success path) or refused it (bad signature, clone
  detected, subject mismatch, expired, unknown credential, any
  verifier-library rejection). The rate-limit short-circuit
  (`PasskeyLoginLockout`) is the sole exception: it fires before
  any DB read, so the row stays valid for the same caller to
  finish once the throttle bucket drains inside the 10-min TTL.
  The delete on failure runs on a **fresh** Unit-of-Work so it
  lands even though the primary UoW rolls back on the raise, and
  it's idempotent (`DELETE ... WHERE id = ?`) so two concurrent
  finish calls racing to consume the same challenge both exit
  cleanly. Scope caveat: as of cd-qx1f the delete-on-failure is
  wired into the HTTP router surface only. The internal callers
  `complete_signup` / `complete_recovery` invoke `register_finish*`
  inside their own UoWs and still leak the row to the 10-minute
  TTL on failure — tracked as a follow-up (cd-wgbl).

## Chat-channel bindings

Bindings created via the chat gateway (§23) are **not credentials**.
A `chat_channel_binding` authenticates the transport a user picked
to talk to their own agent; every write still flows through a
**delegated token** that was minted from the user's session. A
valid inbound message from a future bound external channel does **not**
grant the sender any authority beyond that user's `role_grants`
and never creates a session.

- The binding's link ceremony (§23 "Link ceremony") stores a one-
  time 6-digit code as argon2id hash alongside passkey credentials
  and API tokens; hash parameters are the same family as
  `break_glass_code` (§03).
- An inbound message whose sender address does not match any
  `state = 'active'` binding is silently dropped (§23 "Routing").
- `PAUSE` and `STOP` keywords revoke or suspend a binding without
  any session — they are transport-level opt-outs and do not
  affect the underlying `users` row.
- Re-enrollment (§03 "Re-enrollment side-effects") does **not**
  automatically revoke chat-channel bindings: the phone is
  commonly unchanged when a passkey is lost. Revoking a binding on
  a stolen phone is an explicit action (§23 "Security").

## Privacy

- We store only: credential ID, public key, sign count, AAGUID,
  transport hints, nickname, last_used_at.
- We never store a device fingerprint beyond AAGUID (which only
  identifies the authenticator model).
- `last_used_at` is visible only to the owning user.

## Demo sessions

Demo deployments (§24) do not use passkeys, magic links, or `sessions`
rows. Authority on the demo comes from a **signed demo cookie** that
binds the browser to one or more ephemeral demo workspaces and picks a
seeded persona inside each.

- Cookie name: `__Host-crewday_demo`.
- Flags: `Secure; HttpOnly; SameSite=None; Path=/; Partitioned;
  Max-Age=2592000` (30 days). `SameSite=None` and `Partitioned`
  together opt into CHIPS so the cookie works inside cross-origin
  iframes on the landing page while remaining partitioned per
  top-frame origin. See §15 "Demo deployment".
- Payload: an `itsdangerous`-signed JSON blob with a list of
  `(scenario, workspace_id, persona_user_id)` bindings. See §24
  "Demo cookie".
- Signing key: `CREWDAY_DEMO_COOKIE_KEY`, 32 bytes base64. Rotating
  the key invalidates every live demo cookie on the next request,
  which simply causes a reseed.

The demo cookie is **not** a `sessions` row and does not appear in
the revocation/rotation surfaces described above. It is not a
credential — tampering invalidates the signature, the server treats
it as absent, and a fresh workspace is minted.

Routes that would produce authenticating side effects in prod all
return `501` with `error = "demo_disabled"` when
`CREWDAY_DEMO_MODE=1`:

- Every passkey ceremony endpoint (`/auth/passkey/*`).
- Magic-link send and consume (`/auth/magic/*`).
- API token creation — scoped and delegated alike
  (`POST /api/v1/auth/tokens`). The embedded chat agents (§11) on
  demo acquire their delegated-token equivalent through the demo
  cookie's persona binding, bypassing the normal mint path.
- Break-glass code generation.
- The interactive-session-only endpoints (§11) stay refused —
  their response bodies contain real secrets in prod and there is
  nothing demo-equivalent to return.

Audit still works: every demo write lands an `audit_log` row with
`actor_kind = 'user'`, `actor_id = persona_user_id`, and
`agent_label = 'demo'` so the trail is coherent inside the workspace
for its lifetime. Rows are garbage-collected with the workspace
(§24).
