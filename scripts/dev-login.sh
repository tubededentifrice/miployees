#!/usr/bin/env bash
# Dev-only thin wrapper around scripts/dev_login.py — hard-gated on
# CREWDAY_DEV_AUTH=1. Prints a ``-b '__Host-crewday_session=<value>'``
# flag on stdout (``--output curl`` by default) so callers can paste it
# straight into a ``curl`` command:
#
#     cookie="$(CREWDAY_DEV_AUTH=1 ./scripts/dev-login.sh me@dev.local dev)"
#     curl -sS $cookie http://127.0.0.1:8100/w/dev/api/v1/auth/tokens
#
# Two invocations are supported:
#
#   1. Inside the dev stack (recommended — no host-side Python deps):
#
#        docker compose -f mocks/docker-compose.yml exec app-api \
#          python -m scripts.dev_login --email me@dev.local --workspace dev
#
#      The compose file already sets CREWDAY_DEV_AUTH=1 +
#      CREWDAY_PROFILE=dev inside the container, so the three gates
#      (flag + profile + sqlite) are green by construction. The ``-m``
#      form is required — ``python scripts/dev_login.py`` puts
#      ``scripts/`` on sys.path instead of the repo root and the
#      ``from app.…`` imports miss.
#
#   2. Host-side (requires ``uv sync`` or ``pip install -e .`` so
#      sqlalchemy / click are importable):
#
#        CREWDAY_DEV_AUTH=1 ./scripts/dev-login.sh me@dev.local dev
#
# Respect ``PYTHON=`` to point at an alternative interpreter (a venv
# or a uv-run shim). The default is ``python3`` because every modern
# Linux / macOS ships python3 on PATH, while the legacy ``python``
# symlink may be missing (pyenv-shimmed hosts, Debian 12+, Alpine,
# uv-managed venvs). Passing extra flags after the two required ones
# threads them through verbatim — pick a different ``--output`` or
# override ``--role``:
#
#     ./scripts/dev-login.sh me@dev.local dev --output cookie
#     ./scripts/dev-login.sh me@dev.local dev --role worker
#
# See docs/specs/03-auth-and-tokens.md §"Sessions" and
# scripts/dev_login.py for the row lifecycle.

set -euo pipefail

if [[ "${CREWDAY_DEV_AUTH:-0}" != "1" ]]; then
  echo "error: dev-login requires CREWDAY_DEV_AUTH=1 in your environment" >&2
  exit 1
fi

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <email> <workspace-slug> [--output cookie|json|curl|header] [...]" >&2
  exit 2
fi

# Pick the first Python on PATH. Prefer the caller-supplied $PYTHON,
# then ``python3`` (the canonical name on every modern distro), then
# the legacy ``python`` symlink as a last resort. To run under uv,
# wrap the whole script: ``uv run ./scripts/dev-login.sh ...`` puts
# uv's managed interpreter on PATH so the plain ``python3`` probe
# resolves correctly. If nothing is available we surface a focused
# error instead of execing into nothing.
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "error: dev-login requires python3 on PATH (or \$PYTHON set)" >&2
  exit 127
fi

exec "$PY" -m scripts.dev_login \
  --email "$1" \
  --workspace "$2" \
  --output curl \
  "${@:3}"
