# 13 — CLI (`crewday`)

Per the user's direction: **the CLI is the most important agent
interface**. It is a thin client over the REST API (§12) — no
server-side logic, no local DB, no local state beyond config profiles.
Every command can be executed against a remote deployment.

## Agent-first invariant

**Every human UI action is also a CLI command.** There is no
owner/manager- or worker-facing verb in §14 that cannot be invoked
from this CLI (or its underlying REST endpoint in §12). The
owner/manager-side and worker-side embedded chat agents in §11
expose the CLI + REST surface as their tool set — so anything a
human can do in the UI, an agent can do from chat, subject to the
approval gates in §11.

See §11 "The agent-first invariant" for the broader principle and
§14 for how each UI surface maps back to a command.

## Distribution

- Python 3.12+; installable via `pipx install crewday` (preferred)
  or `uvx crewday`.
- Single wheel, no native deps.
- Static binary via `pyapp` for macOS/Linux/Windows (optional, v1.1).

## Config

Profiles live in `~/.config/crewday/config.toml`:

```toml
default_profile = "prod"

[profile.prod]
base_url = "https://ops.example.com/api/v1"
token = "env:CREWDAY_TOKEN_PROD"
timezone = "Europe/Paris"              # used to resolve ambiguous local times

[profile.dev]
base_url = "http://127.0.0.1:8000/api/v1"
token = "env:CREWDAY_TOKEN_DEV"
```

- `token` values prefixed with `env:` resolve to environment variables
  (avoids storing secrets in the config file).
- Profile selection: `--profile <name>` or `CREWDAY_PROFILE` env
  var.
- `crewday login` writes a new profile: walks through base URL,
  pastes token, pings `/healthz`.

## CLI generation from OpenAPI

### Single source of truth

The FastAPI route definition — its `operation_id`, Pydantic
request/response models, and `x-cli` extension (§12) — is the
canonical CLI definition. The CLI does not maintain a parallel command
list. Adding an endpoint with its `x-cli` metadata is sufficient to
make it appear in the CLI; no hand-written click command is needed for
standard CRUD.

### Surface descriptor (`_surface.json`)

A build step (`python -m cli.codegen`) imports the FastAPI app, calls
`app.openapi()`, walks every operation, merges `x-cli` metadata with
inferred OpenAPI params, and writes `cli/crewday/_surface.json`.
This file is committed and CI-verified (same pattern as
`docs/api/openapi.json`). If the committed copy diverges from a fresh
generation, the `cli-parity` gate (§17) fails the build.

### Runtime command construction

At import time, the CLI loads `_surface.json` and dynamically builds
click groups and commands. Each generated command:

- Constructs the URL from the path template + path params.
- Assembles query/body params from CLI flags.
- Calls `_client.request()`.
- Pipes the response through `_output.format()`.

No per-endpoint Python code is needed for standard CRUD.

### Overrides (`cli/crewday/_overrides/`)

Hand-written click commands for cases the generic path cannot handle:

- `admin.py` — host-CLI-only commands (no HTTP surface): `init`,
  `recover`, `rotate-root-key`, `backup`, `restore`, `purge`,
  `version`.
- `expenses.py` — `expenses submit` composite (autofill + create +
  submit in one flow).
- `tasks.py` — `tasks complete` with multipart photo upload.
- `auth.py` — `auth login` interactive flow.

Each override uses
`@cli_override("group", "verb", covers=["op.id1", "op.id2"])` to
register which `operationId`s it handles, so the parity gate still
sees them as covered.

### Exclusions (`cli/crewday/_exclusions.yaml`)

Endpoints intentionally omitted from CLI generation, each with a
mandatory reason. Canonical list:

- `auth.webauthn.begin_registration`, `auth.webauthn.finish_registration`,
  `auth.webauthn.begin_login`, `auth.webauthn.finish_login` — browser-only
  passkey ceremony.
- `files.blob` — returns a 302 redirect or binary stream, not JSON;
  file metadata is available via the generated `files show` command.
- `healthz`, `readyz`, `version.get` — no-auth infrastructure probes;
  `crewday admin version` covers the operational use case.

Adding an exclusion without a reason fails CI lint.

### `--help` generation

Help text is assembled from the OpenAPI `summary` (one-line) +
parameter descriptions. The `x-cli.summary` field overrides when the
OpenAPI summary is too API-centric. Budget: each command's `--help`
fits in ~200 lines. Global flags are on `crewday --help` only, not
repeated per command.

### Discoverability for agents

An agent can explore the full CLI surface with:

- `crewday --help` — lists all groups.
- `crewday <group> --help` — lists all verbs in a group.
- `crewday <group> <verb> --help` — full param list and examples.
- `crewday surface --json` — dumps the entire `_surface.json` for
  programmatic discovery (one command to learn everything).

### Confirmation cards (`x-agent-confirm`)

For every mutating operation, `_surface.json` carries the
`x-agent-confirm` extension from §12 when present. This is the
**single source of truth** for the confirmation card that surfaces
in the user's chat when their agent approval mode asks for it
(§11 "Per-user agent approval mode"). The CLI itself does not
prompt — delegated-token requests are gated by the REST
middleware, which reads the same annotation. Re-declaring
per-command confirmation copy in the CLI is explicitly avoided:
authors maintain one template per route, used everywhere.

Non-delegated callers (human running `crewday` with a scoped
token or a passkey session) never see these cards; they are not
the subject of the per-user gate.

## Global flags

| flag                | meaning                                            |
|---------------------|----------------------------------------------------|
| `--profile`         | pick a profile                                     |
| `--base-url`        | ad-hoc override                                    |
| `--token`           | ad-hoc override (discouraged; use env)             |
| `-o, --output`      | `json` (default) \| `yaml` \| `table` \| `ndjson`  |
| `--jq <expr>`       | jq-filter the JSON output client-side              |
| `--dry-run`         | resolve args server-side, do not commit            |
| `--explain`         | print the HTTP request that would be sent          |
| `--idempotency-key` | override server-chosen idempotency key             |
| `--correlation-id`  | propagate an agent's correlation id                |
| `--agent-reason`    | sets `X-Agent-Reason` for audit log                |
| `--conversation-ref`| sets `X-Agent-Conversation-Ref` for audit tracing  |
| `--verbose`         | debug log to stderr                                |
| `--no-color`        | for pipes                                          |

## Output

- Default `-o json`: pretty-printed JSON to stdout.
- `-o ndjson`: one JSON object per line (streams well through `jq`).
- `-o table`: human-friendly columns with truncation.
- Exit codes:
  - `0` success
  - `1` client error (validation, not found, forbidden, conflict)
  - `2` server error (5xx, network)
  - `3` approval pending
  - `4` rate-limited (after configured retry budget)
  - `5` config error

Errors go to stderr as RFC 7807 JSON when `-o json`, or a short
human line otherwise; `--verbose` adds the request id.

## Command tree

Grouped by resource. Every command is `crewday <group> <verb> [args]`.

> **Note:** The listing below is the expected output of the generation
> pipeline, kept here for human readers of the spec. The authoritative
> source is the `x-cli` extensions on the API routes (§12); if the
> listing drifts, regenerate `_surface.json` and update this section.

```
crewday auth
  login                       # writes/updates a profile
  whoami
  tokens list
  tokens create --name --scopes --expires
  tokens revoke <id>
  tokens rotate <id>

crewday properties
  list [--kind] [--q]
  add "<name>" --tz <iana> --kind <str|residence|vacation>
  show <id>
  update <id> [--name] [--tz] [--currency] [--welcome-wifi-ssid ...]
  archive <id>

crewday areas
  list --property <id>
  add --property <id> "<name>" --kind <kitchen|bath|...>
  update <id> ...
  archive <id>

crewday stays
  list [--property] [--source] [--from] [--to] [--upcoming 14d]
  add --property <id> --check-in <local> --check-out <local> [--name]
  update <id> ...
  welcome-link <id>           # prints URL
  cancel <id>

crewday ical
  list
  add --property <id> --source airbnb --url <url>
  poll <id>                   # manual trigger
  disable <id>

crewday users
  list [--grant-role] [--property] [--state]
  invite "<name>" --email <email> --grant-role <owner|manager|worker|client|guest> [--property <id>...]
  update <id> ...
  magic-link <id>             # (re-)issue
  archive <id>
  reinstate <id>
  approval-mode show          # your own agent approval mode (bypass|auto|strict)
  approval-mode set <mode>    # bypass | auto | strict — self only (see §11)

crewday work-roles
  list
  add --key maid --name "Maid"
  update <id> ...

crewday tasks
  list [--property] [--work-role] [--assignee] [--state] [--on <date>] [--q]
  show <id>
  create "<title>" --property <id> --work-role <slug> --when '<local-datetime>' [--duration 60]
  from-nl "<free text>" [--dry-run] [--commit]
  assign <id> --to <user-id>
  start <id>
  complete <id> [--photo <path>] [--note "..."] [--checklist-all-checked]
  skip <id> --reason "..."
  cancel <id> --reason "..."
  add-comment <id> "<markdown>"

crewday schedules
  list [--property] [--template]
  add --template <id> --property <id> --rrule '<rfc5545>' --at HH:MM [--area <id>]
  preview --template <id> --rrule '...' --for 30d
  pause <id>
  resume <id>
  apply-edits <id>            # apply changes to existing pending tasks

crewday templates
  list
  add "<name>" --work-role <slug> [--duration] [--photo optional|required] [--checklist @file]
  update <id> ...

crewday instructions
  list [--scope global|property|area] [--q]
  add --scope global|property|area --property <id?> --area <id?> \
      --title "<t>" --body @file.md
  publish <id>                # archive previous, activate new
  link <id> --to-template <tpl-id>
  unlink <link-id>
  archive <id>

crewday inventory
  list [--property] [--low-stock]
  add --property <id> "<name>" --unit each --reorder-point 2 --reorder-target 10
  restock <item-id> --qty 12 [--unit-cost 250]
  adjust <item-id> --to 7 --reason "counted"
  burn-rate --days 30

crewday shifts
  clock-in [--property <id>]
  clock-out <id?>
  list [--user] [--from] [--to]

crewday pay
  rules list [--employee]
  rules set --work-engagement <id> --hourly 1500 --currency EUR --overtime-after 40
  periods list
  periods lock <id>
  payslips list [--user] [--period]
  payslips show <id>
  payslips issue <id>
  payslips mark-paid <id>

crewday expenses
  submit --user <id?> --photo <path> [--vendor "..."] [--amount 1234 --currency EUR]
                                      # autofill from receipt if photo only
  list [--user] [--state]
  approve <id>                        # snaps claim.currency → workspace default + → destination currency (§09)
  reject <id> --reason "..."
  pending-reimbursement [--user]      # approved-but-not-reimbursed totals by owed_currency

crewday rates
  show [--as-of YYYY-MM-DD] [--quote USD] [--source ecb|manual|stale_carryover]
  refresh                              # force today's run of refresh_exchange_rates for this workspace
  set-manual --base EUR --quote XAF --as-of YYYY-MM-DD --rate 655.957  # errors if an ECB row exists

crewday issues
  report --property <id> [--area <id>] "<title>" --body @issue.md \
         [--severity low|normal|high|urgent] \
         [--category damage|broken|supplies|safety|other]
  list [--state] [--property] [--severity] [--category]
  resolve <id> --note "..."
  convert-to-task <id> --work-role handyman

crewday asset-types
  list [--workspace <id>]
  add "<name>" [--icon <slug>] [--default-condition <enum>] \
      [--default-action-interval <days>]
  update <id> ...
  archive <id>

crewday assets
  list [--property] [--type] [--status] [--condition] [--custodian]
  add "<label>" --type <id> --property <id> \
      [--unit <id>] [--area <id>] [--serial <s>] [--purchased-on <date>] \
      [--purchase-price <amount> --currency EUR] \
      [--warranty-expires-on <date>] [--guest-visible]
  show <id>
  update <id> ...
  assign <id> --custodian <user-id>
  unassign <id>
  transfer <id> --to-property <id> [--note "..."]
  condition <id> --set <new|good|fair|poor|needs_replacement> [--note "..."]
  status <id> --set <active|in_repair|decommissioned|disposed> [--note "..."]
  qr-print <id> [--size <label>]

crewday asset-actions
  list --asset <id>
  add --asset <id> --kind <maintenance|inspection|replacement> \
      "<label>" [--interval-days <int>] [--template <tpl-id>]
  activate <action-id>
  perform <action-id> [--note "..."] [--photo <path>]
  schedule-link <action-id> --schedule <sched-id>

crewday documents
  list [--asset] [--property] [--kind] [--expiring-within 30d]
  add --kind <manual|warranty|receipt|insurance|certificate|other> \
      --file <path> [--asset <id>] [--property <id>] \
      [--expires-on <date>] [--issuer "..."]
  show <id>
  download <id> [-o <path>]
  archive <id>

crewday webhooks
  list
  add --name <n> --url <u> --events task.completed,stay.upcoming
  replay <id> --since 2026-04-10

crewday llm
  assignments list
  assignments set <capability> --model google/gemma-4-31b-it [--provider openrouter]
  calls list [--capability] [--from] [--to]

crewday agent-prefs
  show workspace                                 # dump workspace blob (any grant may read)
  show property <id>                             # dump property blob
  show me                                        # dump your own user blob
  set workspace --body @prefs.md [--note "..."]  # gated by agent_prefs.edit_workspace
  set property <id> --body @prefs.md [--note "..."]
  set me --body @prefs.md [--note "..."]         # self-only
  clear workspace | property <id> | me           # empties the blob (still counts as a save / revision)
  revisions workspace | property <id> | me       # list history
  revisions diff <pref-id> <rev-a> <rev-b>

crewday approvals
  list [--state pending]
  show <id>
  approve <id> [--note]
  reject <id> --note "..."

crewday export
  timesheets --from 2026-04-01 --to 2026-04-30 [-o csv]
  payroll --period <id>
  expenses --from ... --to ...
  tasks --from ... --to ...

crewday audit
  tail [--actor-kind] [--action] [--follow]
  export --from --to

crewday admin
  init --email <owner-email>                  # bootstrap (§16)
  recover --email <owner-email>               # emit magic link to stdout
  rotate-root-key --new-key-file <path> | --new-key-stdin
  backup --to <path>
  restore --from <path>
  purge --dry-run                             # GDPR hard-delete flow
  version

crewday surface
  --json                            # dump _surface.json for programmatic discovery
```

### Host-CLI-only admin commands vs interactive-session-only endpoints

Two distinct security classes coexist in this CLI — easy to confuse,
important to keep separate:

1. **Host-CLI-only admin commands.** No HTTP surface at all, agent
   or human. The verbs below are only callable from
   `crewday admin …` on the deployment host, with shell access to
   the running service's environment. The approval pipeline (§11)
   does not apply because there is no request to intercept. v1
   members:

   - `crewday admin rotate-root-key` — envelope-key rotation
     (§15).
   - `crewday admin recover` — offline lockout magic-link
     issuance (§03).
   - `crewday admin purge` — hard-delete per-person payload
     (§02, §15).

2. **Interactive-session-only endpoints.** These **do** have an HTTP
   surface, but they refuse all bearer tokens (scoped and delegated)
   unconditionally. Approval does not help, because the approval
   pipeline would persist the decrypted response in
   `agent_action.result_json`. v1 member:

   - `POST /payslips/{id}/payout_manifest` — full decrypted
     account numbers for treasury use (§09).

   The CLI exposes the endpoint (a manager can still curl/CLI it
   from their workstation with a passkey session), but bearer tokens
   — including delegated agent tokens — are refused. See §11
   "Interactive-session-only endpoints" for the canonical list and
   rationale.

These two classes together form the **short-list of verbs that
require direct human presence** — everything else is reachable
by agents via delegated tokens, subject to the approval gates in
§11.

## Streaming and piping

`audit tail --follow` and `calls list --follow` hold long polls
returning ndjson; any `list` verb accepts `-o ndjson` for `jq`
piping. Example: `crewday audit tail --follow -o ndjson | jq .action`.

## Completion

Click-generated `bash | zsh | fish` completions; `crewday
completion install --shell zsh` writes the file. Remote lookups
use a short-TTL local cache.

## Agent UX conventions

- Data to stdout (JSON by default); errors to stderr as RFC 7807 so
  agents can parse stdout without filtering.
- `--dry-run` returns the resolved request the server would execute,
  so agents can plan multi-step flows without touching state.
- `--explain` dumps the underlying HTTP call (method, URL,
  redacted headers, body) to stderr.
- `--agent-reason` / `--conversation-ref` populate audit headers on
  mutating commands; agents should set them.

## Man pages

`make man` generates roff pages via `click-man`; installed by
`pipx` into the standard location.
