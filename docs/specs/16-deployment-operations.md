# 16 — Deployment and operations

Two supported recipes; you pick one and stick with it.

## Recipe A — Single container (SQLite)

Target: a household with ≤10 employees, ≤10 properties. Runs fine on a
$5/mo 1 vCPU / 1 GB RAM VPS.

### Image

- Base: `python:3.12-slim-bookworm` with digest pin.
- Non-root user `miployees:10001`.
- Single `ENTRYPOINT ["miployees-server"]` binary-like wrapper that
  switches on subcommand (`serve`, `worker`, `admin`).
- Worker runs **in-process** by default; no separate container needed.

### Compose snippet (even though it's a "single container")

```yaml
services:
  app:
    image: ghcr.io/<org>/miployees:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment:
      MIPLOYEES_DATABASE_URL: "sqlite+aiosqlite:///data/miployees.db"
      MIPLOYEES_DATA_DIR: "/data"
      # Bind to 0.0.0.0 *inside the container* so Docker's port map can
      # reach the app. The §15 bind guard is strict: we set
      # ALLOW_PUBLIC_BIND=1 explicitly here, alongside the `ports:`
      # mapping below which is what actually limits reachability to
      # the host loopback.
      MIPLOYEES_BIND: "0.0.0.0:8000"
      MIPLOYEES_ALLOW_PUBLIC_BIND: "1"
      MIPLOYEES_ROOT_KEY: "${MIPLOYEES_ROOT_KEY}"
      MIPLOYEES_PUBLIC_URL: "https://ops.example.com"
      SMTP_HOST: "..."
      SMTP_USER: "..."
      SMTP_PASS: "..."
      MAIL_FROM: "miployees <ops@example.com>"
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
`MIPLOYEES_TRUSTED_INTERFACES` (default `tailscale*`, replaced
wholesale when set); CGNAT ranges are not trusted by CIDR:

```
MIPLOYEES_BIND: "127.0.0.1:8000"     # always passes
MIPLOYEES_BIND: "100.x.y.z:8000"     # passes if that address is on tailscale0
```

### Backup

- SQLite is a single file. The `miployees admin backup` command:
  1. Runs `PRAGMA wal_checkpoint(FULL)`.
  2. Copies `miployees.db`, `data/files/`, and the encrypted
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
0 3 * * * cd /srv/miployees && docker compose exec -T app miployees admin backup --to /backups/
```

**Lockout recovery** is host-CLI-only in v1. If the last manager is
locked out, the operator stops the service, runs
`miployees admin recover --email ...` on the host, and opens the
magic link printed to stdout. SaaS / hosted-operator recovery flows
are out of scope (see §03 recovery paths).

## Recipe B — Compose full-stack (Postgres + MinIO + Caddy)

Target: households with >10 staff, >10 properties, or those who want
object storage, managed TLS, and a smoother path to self-hosted
fallback.

### Services

The `app` and `worker` services share an environment block via the
`x-app-env` YAML anchor. The `app` service does **not** publish a host
port — Caddy reaches it over Docker's internal network at `app:8000`.

```yaml
x-app-env: &app-env
  MIPLOYEES_DATABASE_URL: "postgresql+asyncpg://miployees:${PG_PASS}@db:5432/miployees"
  MIPLOYEES_DATA_DIR: "/data"
  # 0.0.0.0 is bound *inside the container only*; the app service
  # publishes no host port, so reachability is strictly Caddy →
  # app:8000 on the internal compose network. The §15 guard is strict
  # and requires the opt-in below to be explicit.
  MIPLOYEES_BIND: "0.0.0.0:8000"
  MIPLOYEES_ALLOW_PUBLIC_BIND: "1"
  MIPLOYEES_ROOT_KEY: "${MIPLOYEES_ROOT_KEY}"
  MIPLOYEES_PUBLIC_URL: "https://ops.example.com"
  MIPLOYEES_STORAGE: "s3"
  AWS_ACCESS_KEY_ID: "${MINIO_USER}"
  AWS_SECRET_ACCESS_KEY: "${MINIO_PASS}"
  S3_ENDPOINT: "http://minio:9000"
  S3_BUCKET: "miployees"
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
    networks: [miployees]

  app:
    image: ghcr.io/<org>/miployees:<tag>
    restart: unless-stopped
    user: "10001:10001"
    environment: *app-env
    depends_on:
      db:
        condition: service_healthy
      minio:
        condition: service_healthy
    networks: [miployees]

  worker:
    image: ghcr.io/<org>/miployees:<tag>
    command: ["miployees-server", "worker"]
    environment: *app-env
    depends_on: [db, minio]
    networks: [miployees]

  db:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: miployees
      POSTGRES_PASSWORD: ${PG_PASS}
      POSTGRES_DB: miployees
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U miployees"]
      interval: 5s
      timeout: 5s
      retries: 20
    volumes:
      - db-data:/var/lib/postgresql/data
    networks: [miployees]

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
    networks: [miployees]

volumes:
  db-data:
  minio-data:
  caddy-data:
  caddy-config:

networks:
  miployees:
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

## Bootstrap (both recipes)

```
# Generate a root key once and keep it SAFE.
export MIPLOYEES_ROOT_KEY=$(python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")

# First boot:
docker compose up -d
docker compose exec app miployees admin init --email owner@example.com

# The command prints a magic link URL. Open on your phone to register.
```

## Environment variables (selected)

| var                         | default                        | notes                    |
|-----------------------------|--------------------------------|--------------------------|
| `MIPLOYEES_DATABASE_URL`    | sqlite+aiosqlite:///./data/db  |                          |
| `MIPLOYEES_DATA_DIR`        | `./data`                       |                          |
| `MIPLOYEES_BIND`            | `127.0.0.1:8000`               |                          |
| `MIPLOYEES_PUBLIC_URL`      | -                              | required for link building |
| `MIPLOYEES_ROOT_KEY`        | -                              | required                 |
| `MIPLOYEES_STORAGE`         | `local`                        | or `s3`                  |
| `MIPLOYEES_WORKER`          | `inprocess`                    | or `external`            |
| `MIPLOYEES_SESSION_IDLE_DAYS`| 14                            |                          |
| `MIPLOYEES_SESSION_ABS_DAYS`| 30                             |                          |
| `MIPLOYEES_ALLOW_PUBLIC_BIND`| 0                              | required for any bind that isn't loopback or on a trusted interface; compose recipes set it explicitly |
| `MIPLOYEES_TRUSTED_INTERFACES`| `tailscale*`                  | comma-separated fnmatch globs of interface names whose addresses pass without the opt-in; set value **replaces** the default (no implicit baseline) |
| `SMTP_*`                    | -                              | see §10                  |
| `OPENROUTER_API_KEY`        | -                              | see §11                  |

A full env reference lives in `deploy/.env.example`.

## Migrations

- Alembic runs at server start (`alembic upgrade head`) guarded by
  advisory lock (Postgres) or file lock (SQLite) to make N parallel
  containers safe.
- On a major version upgrade, the app refuses to start if the schema
  revision range includes an annotated `requires-backfill` migration;
  the admin runs `miployees admin migrate --with-backfill` which
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
- Log level controlled via `MIPLOYEES_LOG_LEVEL`.

### Metrics

- Prometheus-format at `GET /metrics` (manager-scope API token
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

- The app binds to `tailscale0` naturally if `MIPLOYEES_BIND` is set
  to `<tailscale-ip>:8000`.
- The compose recipe documents a `tailscale` sidecar for hosts that
  prefer to keep Caddy off the public Internet.
