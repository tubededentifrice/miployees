# 04 — Deployment and security

How the site is deployed, what the SaaS operator runs, what the
self-hoster never has to run, and the narrow security posture of
the site surface itself.

## Two deployables, one repo

The site has its own compose stack, its own images, and its own
release cadence. The app can be released without the site; the
site can be released without the app. Rollback is per-deployable.

```
site/
├── docker-compose.yml       # the site stack
├── Caddyfile                # terminates TLS, fronts both services
├── web/                     # built with `npm run build` → static
└── api/                     # built into a Python image

app/…                        # untouched; has its own deployment (§16)
demo/…                       # the app image with CREWDAY_DEMO_MODE=1
                             # (app §24); deployed separately as well
```

`site/docker-compose.yml` never references any file outside
`site/`. `app/`'s compose files never reference anything inside
`site/`.

## `site/docker-compose.yml`

```yaml
services:
  site-web:
    image: ghcr.io/<org>/crewday-site-web:<tag>
    restart: unless-stopped
    # Emits only static files; served by caddy below, not by a
    # live process. The image is a file-only bundle.
    volumes:
      - web-dist:/srv/www:ro
    profiles: ["build"]

  site-api:
    image: ghcr.io/<org>/crewday-site-api:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment:
      SITE_DATABASE_URL: "sqlite+aiosqlite:///data/site.db"
      SITE_DATA_DIR: "/data"
      SITE_BIND: "0.0.0.0:8001"
      SITE_PUBLIC_URL: "https://crew.day"
      SITE_ROOT_KEY: "${SITE_ROOT_KEY}"
      SITE_APP_RPC_BASE_URL: "https://app.crew.day/_internal/feedback"
      SITE_APP_RPC_TOKEN: "${SITE_APP_RPC_TOKEN}"
      SITE_FEEDBACK_SIGN_KEY: "${SITE_FEEDBACK_SIGN_KEY}"
      SITE_SMTP_HOST: "..."
      SITE_SMTP_USER: "..."
      SITE_SMTP_PASS: "..."
      SITE_MAIL_FROM: "crew.day <ops@crew.day>"
      SITE_ADMIN_EMAIL: "ops@crew.day"
    volumes:
      - ./data:/data
    expose:
      - "8001"

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - web-dist:/srv/www:ro
      - caddy-data:/data
      - caddy-config:/config
    ports:
      - "100.x.y.z:80:80"        # Tailscale IP in dev; public on SaaS host
      - "100.x.y.z:443:443"
    depends_on:
      - site-api

volumes:
  web-dist:
  caddy-data:
  caddy-config:
```

Notes:

- `site-api` binds `0.0.0.0:8001` **inside the container** only.
  Caddy talks to it on the internal Docker network; no external
  port map.
- `site-web` is a build-time artefact copied into a named volume;
  Caddy serves it as static files. There is no Node server in
  prod.
- The ports block uses the Tailscale IP (per AGENTS.md, per the
  user's CLAUDE.md) in dev. On the SaaS host, the bind address
  moves to the public IP and Caddy takes care of ACME.
- `/data` holds the SQLite DB, the SMTP dead-letter queue, and
  operator-admin CLI state. Backed up nightly; restoration is a
  single file copy.

## Caddyfile

```
crew.day {
    encode zstd gzip

    # Static site: everything that isn't /api/*
    handle {
        root * /srv/www
        try_files {path} {path}/index.html /404.html
        file_server
    }

    # Backend: /api/* → site-api:8001 (stripping the /api prefix)
    handle_path /api/* {
        reverse_proxy site-api:8001
    }

    header {
        Strict-Transport-Security "max-age=63072000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        Cross-Origin-Opener-Policy "same-origin"
        Cross-Origin-Resource-Policy "same-origin"
        Permissions-Policy "geolocation=(), microphone=(), camera=()"
        Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://crew.day; media-src 'self'; frame-src https://demo.crew.day; connect-src 'self' https://crew.day; font-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'self'; form-action 'self';"
    }
}

www.crew.day {
    redir https://crew.day{uri} 301
}
```

CSP notes:

- `frame-src https://demo.crew.day` — only the demo subdomain may
  be iframed by site pages. Nothing else.
- `style-src 'self' 'unsafe-inline'` — Astro emits a small number
  of inline styles for route-scoped CSS. Narrow to the generated
  hashes in a follow-up spec update once the Astro config stabilises.
- `frame-ancestors 'self'` — the site itself is never iframed. The
  demo's own CSP (app §15 "Demo deployment") is the layer that
  lets the demo be iframed from the site, not the other way around.
- `connect-src 'self'` — islands fetch only from `crew.day/api/*`.
  No third-party analytics, no external tracking endpoints.
- `img-src … https://crew.day` — allows the OG image CDN later if
  one is introduced; otherwise could be tightened to `'self' data:`.

## DNS and TLS

- `crew.day`           A / AAAA  → site host (SaaS operator).
- `www.crew.day`       CNAME     → `crew.day` (Caddy 301's to apex).
- `app.crew.day`       A / AAAA  → app host.
- `demo.crew.day`      A / AAAA  → demo host.

TLS: Caddy auto-provisions Let's Encrypt certs for `crew.day` and
`www.crew.day`. No wildcard needed; each surface provisions its
own.

HSTS is `max-age=63072000` (2 years) — same as the app §15. The
preload list decision rides on the app's, not re-litigated here.

## Secret inventory

| Secret | Stored | Rotated |
|--------|--------|---------|
| `SITE_ROOT_KEY` | Secrets manager → env | Annually |
| `SITE_APP_RPC_TOKEN` | Secrets manager → env on both sides | On breach + annually |
| `SITE_FEEDBACK_SIGN_KEY` | Secrets manager → env on both sides (matches `CREWDAY_FEEDBACK_SIGN_KEY`) | On breach + annually |
| `SITE_SMTP_PASS` | Secrets manager → env | Per provider policy |

- `SITE_ROOT_KEY` is a 32-byte master key used to derive the
  `__Host-suggest_session` cookie signing key and future HMAC
  derivatives. Derivation is HKDF-SHA256 over `SITE_ROOT_KEY`
  with a per-purpose `info` string: `info="suggest_session"` for
  the cookie signing key, distinct `info` values (e.g.
  `unsubscribe_link`) for any future derived secret. No salt is
  used. Rotation invalidates every live cookie — visitors re-enter
  via the app.
- `SITE_FEEDBACK_SIGN_KEY` verifies the magic-link token minted
  by the app's `/feedback-redirect` (§03). Rotation is
  coordinated with the app: the app accepts the new key on mint;
  the site accepts both keys during the 24 h grace window via a
  second env var `SITE_FEEDBACK_SIGN_KEY_PREVIOUS` (base64; empty
  outside the grace window). Post-grace, the operator unsets
  `_PREVIOUS` and the site verifies under the current key only.
- **No `SITE_FEEDBACK_HASH_SALT` on the site side.** The salt
  that derives `user_hash` / `workspace_hash` lives only on the
  app (§03 `CREWDAY_FEEDBACK_HASH_SALT`). The site stores the
  pre-hashed values it receives and cannot reverse them.
- Every secret env var is validated at boot: present, non-empty,
  minimum entropy for cryptographic keys (≥ 128 bits of
  randomness, base64-decoded). Boot fails with a clear pointer to
  this section if any is missing.
- **No secret is written to the SQLite DB.** All secrets are
  process-env-only; rotating is a restart.

## Rate-limit and abuse enforcement

Rate limits defined in §02 are enforced at the `site-api` edge
using a sliding-window counter in SQLite (no Redis — the site's
traffic profile does not justify a second datastore).

- Keying: `submitter_user_hash` for submit, `voter_user_hash` for
  vote, `submitter_workspace_hash` for the per-workspace daily
  rail, a per-endpoint counter for admin CLI calls. Every key is
  pseudonymous by construction (app-derived HMACs; see §02 and §03).
- Below-cap responses are served normally; above-cap responses are
  `429` with `Retry-After: <seconds>`.
- Exceeding limits does not page the operator. A dashboard panel
  shows cumulative 429 count per hour; anything anomalous is
  investigated by hand.
- **Ban list:** `site-admin users ban <user_hash>` and
  `site-admin workspaces ban <workspace_hash>` populate a
  `banned_user_hash` / `banned_workspace_hash` table. Verified
  tokens pass, but writes return `403 banned`. Pure operator
  action — no auto-ban in v1.

No `SITE_BLOCK_CIDR` in v1. The IP-based deny-list in earlier
drafts existed to defend an anonymous-submit surface; with the
auth bridge in place the equivalent is a `user_hash` or
`workspace_hash` ban via the CLI.

## Privacy posture

Codified in `/legal/privacy` and reflected in runtime behaviour.
Summary here for reviewers:

- **No cookies on the marketing pages** (`/`, `/why-*`, `/for-*`,
  `/pricing`, `/changelog`, `/legal/*`). Astro's output is HTML
  with no set-cookie header. No analytics cookie, no consent
  banner needed.
- **One auth cookie on `/suggest`** (`__Host-suggest_session`,
  12 h, site-origin only) — set after the app's magic-link
  handshake (§02 "Auth flow"). Used only for submit and vote;
  browsing the board works without it.
- **Submission body is redacted before insert** (§02 "PII
  posture"). Raw pre-redaction text never hits disk.
- **Email is opt-in, used only for operator-triggered
  updates** (§02 "Email posture"). Unsubscribe is one click,
  signed link.
- **No user or workspace identity ever enters the site.** The
  app derives `user_hash` / `workspace_hash` before signing the
  token; the site receives opaque hashes. No IP logging on the
  submit/vote path.
- **No third-party embedded assets** on marketing pages — fonts,
  icons, images all self-hosted. Nothing calls Google Fonts, no
  "share on X" script, no analytics SDK.
- **No `X-Forwarded-For` log of the client IP**. Caddy access
  logs are disabled on the static routes; `site-api`'s access
  log replaces the IP with its HMAC before write.
- **Backup retention**: `site.db` nightly snapshot, 30 days;
  purged on rotation of the app's `CREWDAY_FEEDBACK_HASH_SALT` so
  older hashes can't be cross-indexed against new ones.

## Observability

- `site-api` writes structured JSON logs to stdout; Caddy logs
  per above. Both are collected by the operator's log stack (out
  of scope for this spec — matches app §16 posture).
- `GET /api/healthz` — liveness probe, 200 if the DB is
  reachable.
- `GET /api/readyz` — readiness probe, 200 if the last
  `cluster_run` is < 12 h old or `cluster_run` is empty (fresh
  deploy). Older than 12 h → 503 signals the batch worker is
  stuck.
- `GET /api/version` — returns the `{"site_api": "<tag>",
  "site_web": "<tag>"}` pair for smoke tests.

Metrics (Prometheus-compatible) are exposed on a second internal
port `0.0.0.0:9001` not published to Caddy. Scraped by the
operator's collector on the internal Docker network only.

## Local development

```bash
cd site/
docker compose --profile dev up --build
```

- `site-web` dev profile runs `astro dev` on `127.0.0.1:4321`
  with hot reload.
- `site-api` dev profile runs `uvicorn site_api.main:app
  --reload` on `127.0.0.1:8001`.
- Caddy dev profile binds `127.0.0.1:8000` (not the Tailscale IP)
  and fronts both so the URL scheme matches prod
  (`http://127.0.0.1:8000/api/*` → api, everything else → web).
- `SITE_APP_RPC_BASE_URL` defaults to `http://127.0.0.1:8100/_internal/feedback`
  in dev, pointing at the local app container (`mocks/docker-compose.yml`).
  The site appends `/moderate`, `/embed`, or `/cluster` per call —
  there is no per-endpoint env var.
- Playwright tests live under `site/web/tests/` and run against
  the local stack.

## Threat model summary

| Threat | Control |
|--------|---------|
| PII in free-text submissions escapes to the public board | Redaction pass on insert (§02); agent reformulation replaces verbatim body on every public surface; operator moderation as safety net |
| Scripted flood of spam submissions | Auth required on the submit endpoint (§02, §03); per-user and per-workspace rate limits; agent moderation rejects gibberish; operator `users ban` / `workspaces ban` for persistent abusers |
| Cluster-id enumeration | Cluster ids are ULID; `visibility=hidden` clusters 404 on the public page |
| Replay of a verified magic-link token | Single-use via `used_token_nonce`; 5 min expiry on the `exp` claim; 12 h cap on the derived cookie |
| Magic-link token leak via URL / referrer | Site self-redirects to `/suggest` without the `t=` param; HTML never contains the token; `/feedback-redirect` on the app returns `Cache-Control: no-store` |
| Signing-key rotation without coordination | Grace window: site accepts both keys for 24 h; app starts minting under the new key at rotation time |
| `/feedback-redirect` clicked by a logged-out user | App returns `302 /login?return=/feedback-redirect` — standard session handling |
| Agent on the app is coerced to mint a feedback token | `/feedback-redirect` carries `x-agent-forbidden: true` — delegated tokens reject at the auth middleware |
| RPC token leak | `CREWDAY_FEEDBACK_RPC_ALLOW_CIDR` on the app side (§03); 24 h grace window on rotation |
| XSS via submission body | Bodies are rendered with the framework's default escape (React island, Astro SSR); CSP forbids inline scripts |
| Clickjacking of the form | `frame-ancestors 'self'` denies iframe embedding |
| LLM prompt injection via submission body | Moderation / cluster prompts wrap submissions in a JSON array; the app's §11 guardrails apply |
| Redaction failure (PII leaks into the app) | App re-applies its redaction layer and 422s if anything matches |
| Site DB exfil | `site.db` is filesystem-only; no remote connection; no secrets stored in rows |

## Cross-references

- App §15 — security baseline the site extends.
- App §16 — deployment idioms reused: ports, `ALLOW_PUBLIC_BIND`
  semantics, non-root user, secret validation at boot.
- §02 — PII / email / rate-limit policy (this spec enforces).
- §03 — RPC auth / allowlist details.
