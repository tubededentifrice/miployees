# 13 — CLI (`miployees`)

Per the user's direction: **the CLI is the most important agent
interface**. It is a thin client over the REST API (§12) — no
server-side logic, no local DB, no local state beyond config profiles.
Every command can be executed against a remote deployment.

## Agent-first invariant

**Every human UI action is also a CLI command.** There is no
manager- or employee-facing verb in §14 that cannot be invoked from
this CLI (or its underlying REST endpoint in §12). The manager-side
and employee-side embedded chat agents in §11 expose the CLI + REST
surface as their tool set — so anything a human can do in the UI,
an agent can do from chat, subject to the approval gates in §11.

See §11 "The agent-first invariant" for the broader principle and
§14 for how each UI surface maps back to a command.

## Why CLI-first for agents

Agents driving HTTP directly end up spending hundreds of tokens on
URL construction, headers, error parsing, retry logic. A good CLI
collapses a multi-step HTTP dance into a single string with:

- Named subcommands that map to REST resources.
- `--help` that fits in a ~200-line context window.
- `--json` output (default) and `--yaml` / `--table` alternatives.
- Sensible exit codes.
- Streaming `stdout` suitable for pipes.
- `--dry-run` and `--explain` on mutating commands.

## Distribution

- Python 3.12+; installable via `pipx install miployees` (preferred)
  or `uvx miployees`.
- Single wheel, no native deps.
- Static binary via `pyapp` for macOS/Linux/Windows (optional, v1.1).

## Config

Profiles live in `~/.config/miployees/config.toml`:

```toml
default_profile = "prod"

[profile.prod]
base_url = "https://ops.example.com/api/v1"
token = "env:MIPLOYEES_TOKEN_PROD"
timezone = "Europe/Paris"              # used to resolve ambiguous local times

[profile.dev]
base_url = "http://127.0.0.1:8000/api/v1"
token = "env:MIPLOYEES_TOKEN_DEV"
```

- `token` values prefixed with `env:` resolve to environment variables
  (avoids storing secrets in the config file).
- Profile selection: `--profile <name>` or `MIPLOYEES_PROFILE` env
  var.
- `miployees login` writes a new profile: walks through base URL,
  pastes token, pings `/healthz`.

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

Grouped by resource. Every command is `miployees <group> <verb> [args]`.

```
miployees auth
  login                       # writes/updates a profile
  whoami
  tokens list
  tokens create --name --scopes --expires
  tokens revoke <id>
  tokens rotate <id>

miployees properties
  list [--kind] [--q]
  add "<name>" --tz <iana> --kind <str|residence|vacation>
  show <id>
  update <id> [--name] [--tz] [--currency] [--welcome-wifi-ssid ...]
  archive <id>

miployees areas
  list --property <id>
  add --property <id> "<name>" --kind <kitchen|bath|...>
  update <id> ...
  archive <id>

miployees stays
  list [--property] [--source] [--from] [--to] [--upcoming 14d]
  add --property <id> --check-in <local> --check-out <local> [--name]
  update <id> ...
  welcome-link <id>           # prints URL
  cancel <id>

miployees ical
  list
  add --property <id> --source airbnb --url <url>
  poll <id>                   # manual trigger
  disable <id>

miployees employees
  list [--role] [--property] [--state]
  add "<name>" --email <email> --role <slug> [--property <id>...]
  update <id> ...
  magic-link <id>             # (re-)issue
  archive <id>
  reinstate <id>

miployees roles
  list
  add --key maid --name "Maid"
  update <id> ...

miployees capabilities
  show <employee-id>
  set <employee-id> <key> <on|off|inherit>

miployees tasks
  list [--property] [--role] [--assignee] [--state] [--on <date>] [--q]
  show <id>
  create "<title>" --property <id> --role <slug> --when '<local-datetime>' [--duration 60]
  from-nl "<free text>" [--dry-run] [--commit]
  assign <id> --to <employee-id>
  start <id>
  complete <id> [--photo <path>] [--note "..."] [--checklist-all-checked]
  skip <id> --reason "..."
  cancel <id> --reason "..."
  add-comment <id> "<markdown>"

miployees schedules
  list [--property] [--template]
  add --template <id> --property <id> --rrule '<rfc5545>' --at HH:MM [--area <id>]
  preview --template <id> --rrule '...' --for 30d
  pause <id>
  resume <id>
  apply-edits <id>            # apply changes to existing pending tasks

miployees templates
  list
  add "<name>" --role <slug> [--duration] [--photo optional|required] [--checklist @file]
  update <id> ...

miployees instructions
  list [--scope global|property|area] [--q]
  add --scope global|property|area --property <id?> --area <id?> \
      --title "<t>" --body @file.md
  publish <id>                # archive previous, activate new
  link <id> --to-template <tpl-id>
  unlink <link-id>
  archive <id>

miployees inventory
  list [--property] [--low-stock]
  add --property <id> "<name>" --unit each --reorder-point 2 --reorder-target 10
  restock <item-id> --qty 12 [--unit-cost 250]
  adjust <item-id> --to 7 --reason "counted"
  burn-rate --days 30

miployees shifts
  clock-in [--property <id>]
  clock-out <id?>
  list [--employee] [--from] [--to]

miployees pay
  rules list [--employee]
  rules set --employee <id> --hourly 1500 --currency EUR --overtime-after 40
  periods list
  periods lock <id>
  payslips list [--employee] [--period]
  payslips show <id>
  payslips issue <id>
  payslips mark-paid <id>

miployees expenses
  submit --employee <id?> --photo <path> [--vendor "..."] [--amount 1234 --currency EUR]
                                         # autofill from receipt if photo only
  list [--employee] [--state]
  approve <id>
  reject <id> --reason "..."

miployees issues
  report --property <id> [--area <id>] "<title>" --body @issue.md \
         [--severity minor|major|urgent]
  list [--state] [--property]
  resolve <id> --note "..."
  convert-to-task <id> --role handyman

miployees webhooks
  list
  add --name <n> --url <u> --events task.completed,stay.upcoming
  replay <id> --since 2026-04-10

miployees llm
  assignments list
  assignments set <capability> --model google/gemma-4-31b-it [--provider openrouter]
  calls list [--capability] [--from] [--to]

miployees approvals
  list [--state pending]
  show <id>
  approve <id> [--note]
  reject <id> --note "..."

miployees export
  timesheets --from 2026-04-01 --to 2026-04-30 [-o csv]
  payroll --period <id>
  expenses --from ... --to ...
  tasks --from ... --to ...

miployees audit
  tail [--actor-kind] [--action] [--follow]
  export --from --to

miployees admin
  init --email <owner-email>                  # bootstrap (§16)
  recover --email <manager-email>             # emit magic link to stdout
  rotate-root-key --new-key-file <path> | --new-key-stdin
  backup --to <path>
  restore --from <path>
  purge --dry-run                             # GDPR hard-delete flow
  version
```

### Host-CLI-only admin commands vs interactive-session-only endpoints

Two distinct security classes coexist in this CLI — easy to confuse,
important to keep separate:

1. **Host-CLI-only admin commands.** No HTTP surface at all, agent
   or human. The verbs below are only callable from
   `miployees admin …` on the deployment host, with shell access to
   the running service's environment. The approval pipeline (§11)
   does not apply because there is no request to intercept. v1
   members:

   - `miployees admin rotate-root-key` — envelope-key rotation
     (§15).
   - `miployees admin recover` — offline lockout magic-link
     issuance (§03).
   - `miployees admin purge` — hard-delete per-person payload
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

`audit tail --follow` and `calls list --follow` keep an HTTP/1.1 long
poll open (server returns ndjson). `stays list --output ndjson` pipes
well into `jq`.

Examples:

```
miployees stays list --upcoming 14d -o ndjson | jq 'select(.source=="airbnb")'
miployees audit tail --actor-kind agent -o ndjson --follow | jq .action
miployees tasks list --state overdue -o json | jq '.data[].id' |
    xargs -I{} miployees tasks show {}
```

## Completion

Bash / zsh / fish completions generated via `click`'s
`_<CMD>_COMPLETE` mechanism. `miployees completion install --shell
zsh` writes the relevant file. Remote lookups (property id
autocomplete) use a local short-TTL cache to avoid slow tabs.

## Agent UX conventions

- **Every command prints only the data structure by default; errors to
  stderr.** Agents can parse stdout without filtering.
- **`--dry-run` returns the *resolved* request**, including the
  assignee the server would pick, so agents can plan multi-step
  workflows without touching state.
- **`--explain`** dumps the underlying HTTP call (method, URL,
  headers with token redacted, body) to stderr. Useful for debugging
  and for teaching an agent the mapping to REST.
- **`--agent-reason`** is surfaced in the audit log. Agents should
  set it on every mutating command.
- **`--conversation-ref`** sets `X-Agent-Conversation-Ref`, linking the
  audit entry back to the conversation or prompt that triggered the
  action (opaque, up to 500 chars).

## Error UX

Agent-friendly error:

```json
{
  "ok": false,
  "status": 409,
  "type": "approval_required",
  "detail": "This action requires a manager approval",
  "approval_id": "appr_01J…",
  "retry_after_seconds": null
}
```

Human-friendly error (with `-o table`): a single red line plus a
"rerun with --verbose for details" nudge.

## Man pages

`make man` generates roff pages via `click-man`; shipped in the
wheel and installed by `pipx` into the standard location.
