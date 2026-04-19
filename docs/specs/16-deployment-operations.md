# 16 — Deployment and operations

Two supported recipes; you pick one and stick with it.

> **Scope.** This spec covers the **app** deployments only. The
> managed SaaS also runs a separate marketing-site stack under
> `site/` (landing pages + agent-clustered suggestion box). Its
> deployment lives in [`docs/specs-site/04-deployment-and-security.md`](../specs-site/04-deployment-and-security.md);
> nothing here references it and self-host deployments never
> need it.

## Recipe A — Single container (SQLite)

Target: a workspace with ≤10 workers, ≤10 properties. Runs fine on a
$5/mo 1 vCPU / 1 GB RAM VPS.

### Image

- Base: `python:3.12-slim-bookworm` with digest pin.
- **Runs as a non-root user.** The Dockerfile creates
  `crewday:10001` (fixed uid/gid, no shell, no login) and ends
  with `USER crewday` so every process in the image — server,
  worker, `admin` subcommands — starts unprivileged. The image
  must **never** contain a `USER 0`, `USER root`, or an unset
  `USER` at the final stage; the release build fails CI if the
  resolved runtime user is `0` (see §17 image smoke test).
- No `setuid` / `setgid` binaries are installed. Filesystem under
  `/app` is owned by `crewday:crewday` and `/data` is writable
  only by `crewday`.
- At process start the server and worker verify `os.geteuid() != 0`
  and refuse to run otherwise, with a clear error pointing to this
  section. This catches the case where an operator overrode the
  image's `USER` via `docker run --user 0` or an orchestrator's
  `securityContext.runAsUser: 0`.
- Single `ENTRYPOINT ["crewday-server"]` binary-like wrapper that
  switches on subcommand (`serve`, `worker`, `admin`).
- Worker runs **in-process** by default; no separate container needed.

### Compose snippet (even though it's a "single container")

```yaml
services:
  app:
    image: ghcr.io/<org>/crewday:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment:
      CREWDAY_DATABASE_URL: "sqlite+aiosqlite:///data/crewday.db"
      CREWDAY_DATA_DIR: "/data"
      # Bind to 0.0.0.0 *inside the container* so Docker's port map can
      # reach the app. The §15 bind guard is strict: we set
      # ALLOW_PUBLIC_BIND=1 explicitly here, alongside the `ports:`
      # mapping below which is what actually limits reachability to
      # the host loopback.
      CREWDAY_BIND: "0.0.0.0:8000"
      CREWDAY_ALLOW_PUBLIC_BIND: "1"
      CREWDAY_ROOT_KEY: "${CREWDAY_ROOT_KEY}"
      CREWDAY_PUBLIC_URL: "https://ops.example.com"
      SMTP_HOST: "..."
      SMTP_USER: "..."
      SMTP_PASS: "..."
      MAIL_FROM: "crew.day <ops@example.com>"
      OPENROUTER_API_KEY: "..."
    volumes:
      - ./data:/data
    ports:
      # Published only on the host's loopback; reverse-proxy from there.
      - "127.0.0.1:8000:8000"
```

### TLS

The user provides TLS. Common choices:

- Caddy on the host, `reverse_proxy 127.0.0.1:8000`.
- Nginx / Traefik behind whatever the user has.

If the host uses Tailscale, publish the port on the Tailscale IP
instead of the host loopback:

```yaml
ports:
  - "100.x.y.z:8000:8000"
```

For **bare-metal** installs (no Docker), bind the app process itself
to loopback or a trusted interface. The §15 guard verifies the target
address is assigned to an interface whose name matches a glob in
`CREWDAY_TRUSTED_INTERFACES` (default `tailscale*`, replaced
wholesale when set); CGNAT ranges are not trusted by CIDR:

```
CREWDAY_BIND: "127.0.0.1:8000"     # always passes
CREWDAY_BIND: "100.x.y.z:8000"     # passes if that address is on tailscale0
```

### Backup

- SQLite is a single file. The `crewday admin backup` command:
  1. Runs `PRAGMA wal_checkpoint(FULL)`.
  2. Copies `crewday.db`, `data/files/`, and the encrypted
     `secret_envelope` rows into a tar.zst.
  3. Rotates old backups (keep last 30 daily + 12 monthly, tunable).

v1 writes backups **unencrypted to the local filesystem**. Encryption
at rest, transport to offsite storage, and key management for those
backups are the operator's responsibility (host-volume encryption,
offsite rsync/restic, cloud snapshot policies). We deliberately do not
ship a built-in remote-backup pipeline because the choice is
environment-specific and mis-shipping one is worse than none.

The optional passphrase flag (`--encrypt-with-passphrase`) exists for
ad-hoc transfer scenarios but is **not** used by the default cron.

Cron:

```
# Run from the directory containing your compose.yaml.
0 3 * * * cd /srv/crewday && docker compose exec -T app crewday admin backup --to /backups/
```

**Lockout recovery** is host-CLI-only in v1. If the last owner is
locked out, the operator stops the service, runs
`crewday admin recover --email ...` on the host, and opens the
magic link printed to stdout. SaaS / hosted-operator recovery flows
are out of scope (see §03 recovery paths).

## Recipe B — Compose full-stack (Postgres + MinIO + Caddy)

Target: workspaces with >10 staff, >10 properties, or those who want
object storage, managed TLS, and a smoother path to self-hosted
fallback.

### Services

The `app` and `worker` services share an environment block via the
`x-app-env` YAML anchor. The `app` service does **not** publish a host
port — Caddy reaches it over Docker's internal network at `app:8000`.

```yaml
x-app-env: &app-env
  CREWDAY_DATABASE_URL: "postgresql+asyncpg://crewday:${PG_PASS}@db:5432/crewday"
  CREWDAY_DATA_DIR: "/data"
  # 0.0.0.0 is bound *inside the container only*; the app service
  # publishes no host port, so reachability is strictly Caddy →
  # app:8000 on the internal compose network. The §15 guard is strict
  # and requires the opt-in below to be explicit.
  CREWDAY_BIND: "0.0.0.0:8000"
  CREWDAY_ALLOW_PUBLIC_BIND: "1"
  CREWDAY_ROOT_KEY: "${CREWDAY_ROOT_KEY}"
  CREWDAY_PUBLIC_URL: "https://ops.example.com"
  CREWDAY_STORAGE: "s3"
  AWS_ACCESS_KEY_ID: "${MINIO_USER}"
  AWS_SECRET_ACCESS_KEY: "${MINIO_PASS}"
  S3_ENDPOINT: "http://minio:9000"
  S3_BUCKET: "crewday"
  OPENROUTER_API_KEY: "..."
  SMTP_HOST: "..."

services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks: [crewday]

  app:
    image: ghcr.io/<org>/crewday:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment: *app-env
    depends_on:
      db:
        condition: service_healthy
      minio:
        condition: service_healthy
    networks: [crewday]

  worker:
    image: ghcr.io/<org>/crewday:<tag>
    command: ["crewday-server", "worker"]
    user: "10001:10001"
    environment: *app-env
    depends_on: [db, minio]
    networks: [crewday]

  db:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: crewday
      POSTGRES_PASSWORD: ${PG_PASS}
      POSTGRES_DB: crewday
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U crewday"]
      interval: 5s
      timeout: 5s
      retries: 20
    volumes:
      - db-data:/var/lib/postgresql/data
    networks: [crewday]

  minio:
    image: minio/minio
    command: server /data --console-address ':9001'
    environment:
      MINIO_ROOT_USER: ${MINIO_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_PASS}
    volumes:
      - minio-data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/ready"]
    networks: [crewday]

volumes:
  db-data:
  minio-data:
  caddy-data:
  caddy-config:

networks:
  crewday:
```

### Caddyfile

```
ops.example.com {
    encode zstd gzip
    reverse_proxy app:8000
    log {
        output file /data/access.log
    }
}
```

### Backup

- Postgres: `pg_dump -Fc` nightly.
- MinIO: `mc mirror` to offsite (S3, B2, rsync.net).
- Encrypted `secret_envelope` rows included in pg_dump; root key is
  **not**.

## Recipe C — Demo deployment

Target: an internet-facing public demo (`demo.crew.day`) that
lets unauthenticated visitors try the product against fake data. One
visitor per cookie, one workspace per (cookie, scenario) pair, 24 h
rolling TTL from last activity. See §24 for the full demo-mode spec.

Critical: **demo is its own container with its own database and its
own OpenRouter key.** Prod never reads demo rows and demo never reads
prod rows. The image is the same image as prod; the difference is
entirely env.

### Topology

- **Host:** `demo.crew.day` (subdomain distinct from any prod host).
- **Database:** a dedicated SQLite file under the demo container's
  volume, **or** a dedicated Postgres DB in a dedicated cluster. It
  must not be the same physical store as any prod deployment. The
  bootstrap refuses to start if `CREWDAY_DATABASE_URL` matches any URL
  in `CREWDAY_DEMO_DB_DENYLIST`.
- **Storage:** local filesystem under `/data/demo/` with a
  per-workspace subdirectory (`/data/demo/<workspace_id>/`). S3 is
  out of scope on demo.
- **TLS:** same as prod recipes — Caddy fronts the container on
  443/80, binds to 127.0.0.1:8000 → `demo.crew.day`.

### Compose snippet

```yaml
services:
  demo-app:
    image: ghcr.io/<org>/crewday:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment:
      CREWDAY_DEMO_MODE: "1"
      CREWDAY_PUBLIC_URL: "https://demo.crew.day"
      CREWDAY_DATABASE_URL: "sqlite+aiosqlite:///data/demo.db"
      CREWDAY_DATA_DIR: "/data"
      CREWDAY_BIND: "0.0.0.0:8000"
      CREWDAY_ALLOW_PUBLIC_BIND: "1"
      CREWDAY_ROOT_KEY: "${DEMO_ROOT_KEY}"              # distinct from prod
      CREWDAY_DEMO_COOKIE_KEY: "${DEMO_COOKIE_KEY}"     # 32 bytes base64
      CREWDAY_DEMO_FRAME_ANCESTORS: "https://crew.day https://*.crew.day"
      CREWDAY_DEMO_GLOBAL_DAILY_USD_CAP: "5"
      CREWDAY_DEMO_BLOCK_CIDR: ""                        # optional deny-list
      OPENROUTER_API_KEY: "${DEMO_OPENROUTER_KEY}"       # distinct from prod
      # No SMTP_*; the Mailer port binds to the null adapter under
      # CREWDAY_DEMO_MODE=1 regardless of SMTP_* values.
    volumes:
      - ./data-demo:/data
    ports:
      - "127.0.0.1:8100:8000"
```

The bootstrap guard enforces that `CREWDAY_DEMO_MODE=1` sees a
`CREWDAY_PUBLIC_URL` whose host ends in `.crew.day` **and** a
`CREWDAY_DATABASE_URL` distinct from every URL in
`CREWDAY_DEMO_DB_DENYLIST`. The intent is to make "flipping demo on
in prod by accident" a hard boot failure, not a quiet misconfig.

### Caddyfile

```
demo.crew.day {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8100
    log {
        output file /data/demo-access.log
    }
}
```

### Bootstrap

```
# Root key for demo — DISTINCT from prod. Keep separate.
export DEMO_ROOT_KEY=$(python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
export DEMO_COOKIE_KEY=$(python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
export DEMO_OPENROUTER_KEY="sk-or-…"                  # a demo-only key

docker compose -f demo-compose.yml up -d
```

No `crewday admin init` on demo. There is no bootstrap owner, no
first-boot magic link. The scenarios seed themselves on each visitor's
first request. Scenario fixtures live under `app/fixtures/demo/`
inside the image (§24).

### GC and scheduled jobs on demo

The regular, non-demo job set (shared by recipes A / B / D) is:

- `usage_rollup` — every 60 s; refreshes
  `workspace_usage.cost_30d_usd` (§11 "Workspace usage budget").
- `llm_raw_response_sweep` — every 60 minutes; nulls
  `llm_call.raw_response_json` and `raw_response_expires_at` for rows
  past their TTL (§11 "Cost tracking").
- `sync_llm_pricing` — weekly (cron `0 3 * * 1` UTC); pulls per-
  million token prices from OpenRouter into
  `llm_provider_model.{input,output}_cost_per_million` per §11
  "Price sync". No Redis, no external queue; runs in the same
  `crewday-server worker` process as the other jobs.
- `ical_poll`, `digest_compose`, `anomaly_detect`, `email_retry`,
  `webhook_retry`, `approval_expire` — the rest of the standard
  worker cadence, unchanged.
- `agent_dispatch_sweep` — every 30 s; targets inbound
  `chat_message` rows stuck in the dispatch state machine
  (§23 "Routing > Inbound") on two conditions: `pending` past a
  60 s grace (subscriber never picked up — usually an app-process
  restart between webhook commit and task scheduling) and
  `dispatching` past a 5 min grace (task started but the process
  died before CASing the terminal state). Under the default
  `CREWDAY_WORKER=inprocess` the sweeper re-publishes
  `chat_message.received` on the in-process bus and the app-process
  subscriber retries. Under `CREWDAY_WORKER=external` the scheduler
  runs in a separate process whose bus the app subscriber does not
  see, so the sweeper invokes the agent runtime directly (same DB,
  same adapters). Both paths CAS `agent_dispatch_state` before
  invoking the runtime; a sweep firing while the primary dispatch
  is genuinely still running loses the CAS and no-ops.

The following jobs run on demo in addition to the regular set:

- `demo_gc` — every 15 minutes; purges `demo_workspace` rows whose
  `expires_at < now()` and every dependent row via FK cascade.
- `demo_usage_rollup` — every 60 s; refreshes the per-workspace
  rolling 30-day cost aggregate (§11). Runs alongside the regular
  `usage_rollup`; exists because the demo deployment counts against
  the `CREWDAY_DEMO_GLOBAL_DAILY_USD_CAP` envelope (§11 "Demo mode
  overrides") in addition to per-workspace caps.

The following jobs are **disabled** on demo:

- iCal polling.
- Daily digest composition and anomaly detection.
- Email delivery retries (the Mailer is the null adapter).
- Webhook delivery retries.
- `sync_llm_pricing` — demo defaults to `:free` OpenRouter models
  and the global daily cap; live pricing is not needed.

### Backup on demo

Demo does not need a backup policy. Every workspace is ephemeral and
a lost container is indistinguishable from 15 minutes of GC. The
operator may still snapshot the scenario fixtures (they live in the
image) for reproducibility.

## Recipe D — Managed multi-tenant deployment (Postgres + S3 + Caddy)

A **deployment topology example**, not a mode — the same image
that runs Recipe A runs Recipe D. The only differences are the
backend choices, which the capability registry (§01) probes at
boot: Postgres enables `features.rls`, S3 enables
`features.object_storage`, and so on. Everything else
(multi-tenancy, signup, RLS as defence-in-depth) is unified in
the codebase.

Target: the operator of `crew.day` or any similar managed
deployment where tenants are untrusted strangers. Visitors self-
serve-signup via §03 with `settings.signup_enabled = true`; every
workspace lives in one shared Postgres, isolated at the
application layer by the `workspace_id` filter and at the DB
layer by RLS (§15). Payments are **not** in scope for v1
(§00 N1); every tenant is on the `free` plan with hard caps.

### Topology

```
                Internet
                    |
              Caddy (TLS)    ───  wildcard-capable so per-WS subdomains
                    |            can be added later (§01 "Workspace
                    v            addressing") without re-issuing certs
              app (FastAPI)       — same image as recipes A, B
                    |
       +------------+-----------+
       |            |           |
  Postgres 15     S3 / MinIO   OpenRouter
  (RLS on        (per-WS path) (budgeted per WS)
  every table)
```

- The **app** container is `crewday-server serve`; a **worker**
  container runs `crewday-server worker` as a separate replica so
  signup throttling, SSE fan-out, and LLM calls do not contend.
- **Postgres 15+ recommended** for the threat profile (untrusted
  tenants) because it activates `features.rls` (§01) as defence-
  in-depth. The app runs equally on SQLite; the choice is about
  isolation depth and concurrent-write scalability, not about
  which codepath executes.
- **S3-compatible storage** (MinIO in-cluster or a managed
  bucket) with per-workspace prefixes
  (`s3://crewday-saas/<workspace_id>/uploads/...`).

### Compose snippet

```yaml
services:
  app:
    image: ghcr.io/<org>/crewday:<tag>
    command: ["crewday-server", "serve"]
    restart: unless-stopped
    user: "10001:10001"
    environment:
      CREWDAY_DATABASE_URL: "postgresql+psycopg://crewday:${PG_PASS}@db:5432/crewday"
      CREWDAY_STORAGE: "s3"
      CREWDAY_S3_BUCKET: "crewday-saas"
      CREWDAY_S3_ENDPOINT: "https://s3.<region>.amazonaws.com"
      CREWDAY_BIND: "0.0.0.0:8000"
      CREWDAY_ALLOW_PUBLIC_BIND: "1"
      CREWDAY_ROOT_KEY: "${CREWDAY_ROOT_KEY}"
      CREWDAY_PUBLIC_URL: "https://crew.day"
      SMTP_HOST: "..."
      SMTP_USER: "..."
      SMTP_PASS: "..."
      MAIL_FROM: "crew.day <no-reply@crew.day>"
      OPENROUTER_API_KEY: "..."
    depends_on: [db, worker]
    ports: ["127.0.0.1:8000:8000"]

  worker:
    image: ghcr.io/<org>/crewday:<tag>
    command: ["crewday-server", "worker"]
    restart: unless-stopped
    user: "10001:10001"
    environment:
      CREWDAY_DATABASE_URL: "postgresql+psycopg://crewday:${PG_PASS}@db:5432/crewday"
      CREWDAY_STORAGE: "s3"
      CREWDAY_S3_BUCKET: "crewday-saas"
      CREWDAY_ROOT_KEY: "${CREWDAY_ROOT_KEY}"
      OPENROUTER_API_KEY: "..."
    depends_on: [db]

  db:
    image: postgres:15
    restart: unless-stopped
    environment:
      POSTGRES_USER: "crewday"
      POSTGRES_PASSWORD: "${PG_PASS}"
      POSTGRES_DB: "crewday"
    volumes: ["./pgdata:/var/lib/postgresql/data"]
```

### Caddyfile

```
crew.day {
  reverse_proxy app:8000
  encode zstd gzip
  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    Permissions-Policy "camera=(), microphone=(self), geolocation=(self)"
  }
}

# Optional, for the future subdomain-isolation layer (§01).
# Uncomment once wildcard DNS + DNS-01 TLS is in place.
# *.crew.day {
#   tls {
#     dns route53
#   }
#   reverse_proxy app:8000
# }
```

### RLS installation

Alembic runs `alembic upgrade head` at boot; the final migration in
the suite runs `app/tenancy/install_rls.py`, which creates or
replaces a policy on every table carrying `workspace_id`:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_read ON <table>
    USING (workspace_id = current_setting('crewday.workspace_id')::text);
CREATE POLICY tenant_write ON <table>
    FOR INSERT / UPDATE / DELETE
    WITH CHECK (workspace_id = current_setting('crewday.workspace_id')::text);
```

The migration is idempotent and re-asserts the policy on every
deploy so drift via manual `psql` cannot persist.

### Self-serve signup

Self-serve signup is an operator setting, not an env var (see §01
"Capability registry", §03 for flow, §15 for caps). On a fresh
deployment signup is **off**. To turn it on:

```
docker compose exec app crewday admin settings set signup_enabled true
docker compose exec app crewday admin settings set signup_disposable_domains_path /etc/crewday/disposable-domains.txt
```

Throttle overrides, disposable-domain paths, and pre-verification
caps are also operator settings (`settings.signup_throttle_*`,
`settings.signup_caps_*`); defaults come from §15. Changes take
effect immediately — the settings are read through the capability
registry at every request, not cached at boot.

### Backup

- **Postgres:** `pg_dump --format=custom` nightly to S3 with
  server-side encryption; point-in-time recovery via WAL
  shipping for anything more aggressive.
- **S3 uploads:** lifecycle versioning on the bucket plus
  cross-region replication for the operator's chosen
  durability tier.
- **Per-tenant export** (tenant self-serve): workspace owner can
  request a full export via `POST /w/<slug>/api/v1/admin/export`,
  returning a signed URL to a ZIP of the tenant's rows +
  uploads. Used for GDPR data portability (§15).

### Migration off SaaS (tenant self-rescue)

A tenant may at any time download their export and re-import into
a self-host instance via `crewday admin import <path>` (runs on
Recipe A or Recipe B). The import assigns a new workspace row and
remaps `workspace_id` — the slug is preserved unless it collides.

## Bootstrap

### Self-hosted (recipes A, B)

```
# Generate a root key once and keep it SAFE.
export CREWDAY_ROOT_KEY=$(python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")

# First boot:
docker compose up -d
docker compose exec app crewday admin init --email owner@example.com --slug myhome

# The command prints a magic link URL. Open on your phone to register.
# The workspace is then reachable at https://<host>/w/myhome/today.
```

### Managed multi-tenant (recipe D)

```
# Root key, same idea as above.
export CREWDAY_ROOT_KEY=...
export PG_PASS=...
docker compose up -d

# Migrations run at boot. On Postgres the RLS policies install
# automatically (capability features.rls = true; see §01).

# First operator workspace — same command as self-host, since
# there is only one codepath.
docker compose exec app crewday admin workspace create \
    --slug ops --email ops@example.com

# Open the deployment to public signups:
docker compose exec app crewday admin settings set signup_enabled true

# To promote a workspace to verification_state='trusted' (lifts
# all tight caps; see §02):
docker compose exec app crewday admin workspace trust <slug> --reason "..."

# To close signups again (e.g. beta freeze):
docker compose exec app crewday admin settings set signup_enabled false
```

### Demo (recipe C)

See §24 for the demo-specific bootstrap flow — workspaces are
provisioned per visitor, not per operator.

## Environment variables (selected)

| var                         | default                        | notes                    |
|-----------------------------|--------------------------------|--------------------------|
| `CREWDAY_DATABASE_URL`    | sqlite+aiosqlite:///./data/db  |                          |
| `CREWDAY_DATA_DIR`        | `./data`                       |                          |
| `CREWDAY_BIND`            | `127.0.0.1:8000`               |                          |
| `CREWDAY_PUBLIC_URL`      | -                              | required for link building |
| `CREWDAY_ROOT_KEY`        | -                              | required                 |
| `CREWDAY_STORAGE`         | `local`                        | or `s3`                  |
| `CREWDAY_WORKER`          | `inprocess`                    | or `external`            |
| `CREWDAY_SESSION_IDLE_DAYS`| 14                            |                          |
| `CREWDAY_SESSION_ABS_DAYS`| 30                             |                          |
| `CREWDAY_ALLOW_PUBLIC_BIND`| 0                              | required for any bind that isn't loopback or on a trusted interface; compose recipes set it explicitly |
| `CREWDAY_TRUSTED_INTERFACES`| `tailscale*`                  | comma-separated fnmatch globs of interface names whose addresses pass without the opt-in; set value **replaces** the default (no implicit baseline) |
| `SMTP_*`                    | -                              | see §10                  |
| `OPENROUTER_API_KEY`        | -                              | see §11                  |
| `CREWDAY_DEMO_MODE`         | 0                              | Recipe C only; refuses to boot outside the demo URL allowlist. §24 |
| `CREWDAY_DEMO_COOKIE_KEY`   | -                              | Recipe C only; 32 bytes base64; signs `__Host-crewday_demo`. §24 |
| `CREWDAY_DEMO_FRAME_ANCESTORS` | -                           | Recipe C only; whitespace-separated CSP `frame-ancestors` allowlist. §15 |
| `CREWDAY_DEMO_GLOBAL_DAILY_USD_CAP` | 5                      | Recipe C only; deployment-wide daily kill-switch across every demo workspace. §11 |
| `CREWDAY_DEMO_BLOCK_CIDR`   | -                              | Recipe C only; optional comma-separated IP deny-list. §24 |
| `CREWDAY_DEMO_DB_DENYLIST`  | -                              | Recipe C only; comma-separated list of DB URLs the demo refuses to start on. §16 |
| `CREWDAY_FEEDBACK_URL`      | -                              | Marketing-site bridge: redirect target for `GET /feedback-redirect`; must end in `/suggest`. Set together with the next two or none. See `docs/specs-site/03-app-integration.md`. |
| `CREWDAY_FEEDBACK_SIGN_KEY` | -                              | 32-byte base64 HMAC key used to sign the magic-link token; matches the site's `SITE_FEEDBACK_SIGN_KEY`. See `docs/specs-site/03-app-integration.md`. |
| `CREWDAY_FEEDBACK_HASH_SALT`| -                              | 32-byte base64 salt used to derive the opaque `user_hash` / `workspace_hash` the site keys writes off. App-only — never sent to the site. See `docs/specs-site/03-app-integration.md`. |


Signup behaviour, throttles, and disposable-domain paths are
**deployment settings**, not env vars — see §01 "Capability
registry" and the "Self-serve signup" subsection above.
Environment differences (DB engine, storage backend, LLM
provider) surface via capabilities probed at boot; the code does
not branch on a deployment-mode switch.

A full env reference lives in `deploy/.env.example`.

## Migrations

- Alembic runs at server start (`alembic upgrade head`) guarded by
  advisory lock (Postgres) or file lock (SQLite) to make N parallel
  containers safe.
- On a major version upgrade, the app refuses to start if the schema
  revision range includes an annotated `requires-backfill` migration;
  the admin runs `crewday admin migrate --with-backfill` which
  performs the backfill and then restarts.

## Healthchecks

- `/healthz` — returns 200 if the process is up. No DB query.
- `/readyz` — returns 200 only if DB, storage, and mail send can be
  done (mail check is `NOOP` in `ready=true` handshake, not a real
  send; see implementation).
- `/version` — git sha, release tag, OpenAPI hash.

## Observability

### Logs

- JSON-lines to stdout. Captured by Docker.
- Key fields: `at`, `level`, `event`, `correlation_id`, `actor_*`,
  `token_id`, `path`, `status`, `duration_ms`.
- Log level controlled via `CREWDAY_LOG_LEVEL`.

### Metrics

- Prometheus-format at `GET /metrics` (owner/manager-scope API token
  required; not public).
- Included: HTTP histograms, DB pool gauges, worker-job durations,
  LLM cost counter, email delivery counters, webhook delivery
  histogram.
- Default scrape interval: 30s.

### Traces

- Optional OTLP exporter (`OTEL_EXPORTER_OTLP_ENDPOINT`). Off by
  default; turning it on adds HTTP spans and DB span auto-instrument.

### Dashboards

A `deploy/grafana/` folder ships a reference dashboard JSON for
self-hosted Grafana; documented but not auto-provisioned.

## Upgrades

- Pull new image → `docker compose up -d`.
- Migrations run on start (see above).
- Breaking changes announced in `CHANGELOG.md` with required manual
  steps.
- Roll-back guidance: `docker compose down`, replace tag, keep DB
  file; migrations are either reversible or flagged `lossy` in which
  case the release notes say "only-forward".

## Disaster recovery drill

Documented in `docs/runbooks/dr-drill.md`. Quarterly exercise:

- Stop services on a test host.
- Restore latest backup tar.
- Bring services up with the saved root key.
- Verify login and task list against a known fixture.
- Record time-to-recovery.

## Tailscale

- The app binds to `tailscale0` naturally if `CREWDAY_BIND` is set
  to `<tailscale-ip>:8000`.
- The compose recipe documents a `tailscale` sidecar for hosts that
  prefer to keep Caddy off the public Internet.
