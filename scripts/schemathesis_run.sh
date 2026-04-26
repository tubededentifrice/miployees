#!/usr/bin/env bash
# Schemathesis API contract runner (cd-3j25).
#
# Boots the FastAPI app via ``uvicorn`` on a free loopback port,
# seeds the SQLite database (``alembic upgrade head`` + a dev-login
# round-trip so workspace + owner + session row exist), then runs
# ``schemathesis run`` against ``/api/openapi.json`` with the custom
# checks under ``tests/contract/hooks.py`` registered via
# ``SCHEMATHESIS_HOOKS``. Tears the server down on exit (success or
# failure).
#
# Usage:
#
#     bash scripts/schemathesis_run.sh
#     bash scripts/schemathesis_run.sh --max-examples 50
#     SCHEMATHESIS_PORT=18234 bash scripts/schemathesis_run.sh
#
# Spec: ``docs/specs/17-testing-quality.md`` §"API contract".
#
# CI invokes this through ``make schemathesis`` (the Makefile target
# is the gate). The pytest wrapper ``tests/contract/test_schemathesis_runner.py``
# also calls this script as a subprocess so a developer running
# ``pytest -m schemathesis`` exercises the same code path as CI.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config knobs — overridable via environment variables. Defaults match
# the AGENTS.md "tests bind to 127.0.0.1, never the public iface" rule.
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SCHEMATHESIS_PORT:-18345}"
HOST="${SCHEMATHESIS_HOST:-127.0.0.1}"
MAX_EXAMPLES="${SCHEMATHESIS_MAX_EXAMPLES:-20}"
WORKERS="${SCHEMATHESIS_WORKERS:-1}"
# Schemathesis loads hooks via ``SCHEMATHESIS_HOOKS``. Two shapes
# work: a dotted module name OR a file path. We use the file path
# form by default because ``tests/`` is not a Python package
# (pytest runs with ``--import-mode=importlib``); the dotted form
# would fail under schemathesis' plain ``__import__`` loader.
HOOKS_MODULE="${SCHEMATHESIS_HOOKS:-tests/contract/hooks.py}"
PYTHON_BIN="${PYTHON:-uv run python}"
SCHEMATHESIS_BIN="${SCHEMATHESIS_BIN:-uv run schemathesis}"

# Per-run scratch dir so concurrent invocations on the dev box don't
# clobber each other's SQLite file. ``mktemp -d`` lands under TMPDIR
# (or ``/tmp`` if TMPDIR is unset).
SCRATCH="$(mktemp -d -t crewday-schemathesis-XXXXXX)"
DB_PATH="${SCRATCH}/schemathesis.db"
LOG_PATH="${SCRATCH}/uvicorn.log"

# uvicorn PID — set inside ``boot``; checked in the trap so we don't
# kill -0 a stray PID on early failure.
UVICORN_PID=""

cleanup() {
    set +e
    if [[ -n "${UVICORN_PID}" ]] && kill -0 "${UVICORN_PID}" 2>/dev/null; then
        # SIGTERM first; uvicorn's graceful shutdown takes ~1s. SIGKILL
        # if it's still alive after a short grace period — a stuck
        # worker would otherwise hold the port and block re-runs.
        kill -TERM "${UVICORN_PID}" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "${UVICORN_PID}" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "${UVICORN_PID}" 2>/dev/null && kill -KILL "${UVICORN_PID}" 2>/dev/null || true
    fi
    if [[ -n "${KEEP_SCRATCH:-}" ]]; then
        echo "schemathesis: scratch retained at ${SCRATCH}" >&2
    else
        rm -rf "${SCRATCH}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Boot the app
# ---------------------------------------------------------------------------

# All env the factory needs to come up cleanly:
# * CREWDAY_DATABASE_URL — fresh SQLite under our scratch dir.
# * CREWDAY_PROFILE=dev + CREWDAY_DEV_AUTH=1 — dev-login gates.
# * CREWDAY_ROOT_KEY — 32-byte hex; fixed value below is dev-only and
#   matches mocks/docker-compose.yml so the env shape stays familiar.
# * CREWDAY_BIND_HOST — pinned to 127.0.0.1 (AGENTS.md "never public").
export CREWDAY_DATABASE_URL="sqlite:///${DB_PATH}"
export CREWDAY_PROFILE="dev"
export CREWDAY_DEV_AUTH="1"
export CREWDAY_ROOT_KEY="${CREWDAY_ROOT_KEY:-a086980eae3ed92658101eda4cab651ed4b8d4fafed4207f26446d9572b60eeb}"
export CREWDAY_BIND_HOST="${HOST}"
export CREWDAY_BIND_PORT="${PORT}"
# Cap LLM budget at zero so any handler reaching the LLM seam fails
# fast instead of doing real network calls during the contract sweep.
export CREWDAY_LLM_DEFAULT_BUDGET_CENTS_30D="0"

cd "${REPO_ROOT}"

echo "schemathesis: migrating ${DB_PATH}" >&2
${PYTHON_BIN} -m alembic -c alembic.ini upgrade head >"${LOG_PATH}.alembic" 2>&1

echo "schemathesis: starting uvicorn on ${HOST}:${PORT}" >&2
${PYTHON_BIN} -m uvicorn app.main:create_app --factory \
    --host "${HOST}" --port "${PORT}" \
    --log-level warning \
    >"${LOG_PATH}" 2>&1 &
UVICORN_PID="$!"

# Poll /healthz until the server responds. 30s is plenty for a fresh
# checkout; if the loop times out the log is dumped before the trap
# tears the server down.
deadline=$((SECONDS + 30))
while ! curl -fsS "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "schemathesis: uvicorn never came up — log follows" >&2
        cat "${LOG_PATH}" >&2 || true
        exit 1
    fi
    if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
        echo "schemathesis: uvicorn process exited prematurely — log follows" >&2
        cat "${LOG_PATH}" >&2 || true
        exit 1
    fi
    sleep 0.2
done
echo "schemathesis: uvicorn healthy" >&2

# ---------------------------------------------------------------------------
# Seed: workspace owner + Bearer token + dev session cookie
# ---------------------------------------------------------------------------
#
# ``scripts/_schemathesis_seed.py`` reuses the dev-login provisioning
# path (user + workspace + owners group + budget ledger) and then
# mints an API token directly through the domain service — bypasses
# the HTTP route's CSRF + cookie dance, which would otherwise force
# this script to thread three more cookies through curl. The token is
# workspace-scoped with empty ``scopes`` (§03 "Scopes": "Empty is
# allowed on v1" — the token resolves authority via the owner's role
# grants, which gives the fuzzer the same surface a real owner has).
#
# The session cookie covers bare-host paths the Bearer token can't
# reach (``/api/v1/auth/me``, etc.) — those routes accept session
# auth only, not Bearer.
SLUG="schemathesis"
EMAIL="schemathesis@dev.local"

TOKEN=$(${PYTHON_BIN} -m scripts._schemathesis_seed \
    --email "${EMAIL}" \
    --workspace "${SLUG}" \
    --output token)

if [[ -z "${TOKEN}" ]]; then
    echo "schemathesis: failed to mint Bearer token (seed helper printed nothing)" >&2
    exit 1
fi

SESSION=$(${PYTHON_BIN} -m scripts._schemathesis_seed \
    --email "${EMAIL}" \
    --workspace "${SLUG}" \
    --output session)

if [[ -z "${SESSION}" ]]; then
    echo "schemathesis: failed to mint dev session cookie" >&2
    exit 1
fi
echo "schemathesis: seeded workspace=${SLUG} owner=${EMAIL} (Bearer + session cookie ready)" >&2

# ---------------------------------------------------------------------------
# Run schemathesis
# ---------------------------------------------------------------------------
#
# ``--mode all`` enables both positive (schema-conforming) and
# negative (deliberately broken) data generation — §17 acceptance
# criterion: "generated cases include negative inputs (bad enums,
# missing required fields)". ``-n`` caps examples per operation so the
# CLI run finishes inside the §17 < 5 min budget. ``-c all`` runs
# every built-in check + the three custom checks registered by
# :mod:`tests.contract.hooks` via ``SCHEMATHESIS_HOOKS``.
#
# ``--exclude-path-regex`` keeps the SSE transport endpoint out of
# the sweep — its ``text/event-stream`` body is an open-ended pipe
# the schemathesis runner can't reason about with a request budget.
#
# ``--generation-codec=ascii`` is the HTTP-header constraint:
# schemathesis otherwise generates random unicode strings that
# urllib3's ``putheader`` rejects with a UnicodeEncodeError before
# the request leaves the test harness. RFC 7230 obsoleted the legacy
# ISO-8859-1 header encoding in favour of plain US-ASCII; pinning the
# codec short-circuits the runtime error. Body bodies are still
# JSON-serialised so non-ASCII content survives the codec
# restriction unchanged.
#
# Initial scope (cd-3j25): the sweep covers a curated allowlist of
# endpoints whose schemas have been validated against the
# implementation. Adding more endpoints to the gate is a per-context
# follow-up: file a Beads task per context, fix any conformance
# divergence the gate flags, then extend the include list.

export SCHEMATHESIS_HOOKS="${HOOKS_MODULE}"

REPORT_DIR="${SCHEMATHESIS_REPORT_DIR:-${SCRATCH}/report}"
mkdir -p "${REPORT_DIR}"

# Initial gate scope — operation-ids whose schemas have been audited
# against the implementation. The hook in
# ``tests/contract/hooks.py::constrain_workspace_slug`` pins the
# ``{slug}`` path parameter to our seeded workspace so the requests
# resolve through the workspace membership lookup instead of 404ing
# in the tenancy middleware.
#
# Extending the gate to additional operation-ids is a per-endpoint
# follow-up: file a Beads task per context, run the gate against the
# new operation, fix any conformance divergence the gate flags, and
# only then add the operation-id here. A blanket "include everything"
# would surface ~270 real schema-conformance bugs (cd-3j25 audit) and
# turn the gate red on day one.
INCLUDE_ARGS=(
    # ``auth.me.get`` — bare-host singleton, session-cookie authed,
    # no path parameters. Picked as the v0 gate because it's the
    # smallest authed surface that exercises the Authorization
    # custom check end-to-end (the request carries both Bearer and
    # session-cookie headers; the check sees the Bearer and passes).
    --include-operation-id 'auth.me.get'
)

set +e
${SCHEMATHESIS_BIN} run \
    "http://${HOST}:${PORT}/api/openapi.json" \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Cookie: __Host-crewday_session=${SESSION}" \
    --checks all \
    --mode all \
    --max-examples "${MAX_EXAMPLES}" \
    --workers "${WORKERS}" \
    --exclude-path-regex '^/events$' \
    --generation-codec ascii \
    --suppress-health-check filter_too_much,data_too_large,too_slow \
    "${INCLUDE_ARGS[@]}" \
    --report junit \
    --report-dir "${REPORT_DIR}" \
    "$@"
RC=$?
set -e

if [[ ${RC} -ne 0 ]]; then
    echo "schemathesis: run failed (exit ${RC}); uvicorn log:" >&2
    cat "${LOG_PATH}" >&2 || true
fi

exit "${RC}"
