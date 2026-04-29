#!/bin/sh
set -eu

cmd="${1:-serve}"
if [ "$#" -gt 0 ]; then
  shift
fi

if [ "$(id -u)" = "0" ]; then
  echo "crew.day refuses to run as root; use the image's crewday:10001 user." >&2
  exit 78
fi

ensure_root_key() {
  if [ -n "${CREWDAY_ROOT_KEY:-}" ]; then
    return
  fi

  key_file="${CREWDAY_ROOT_KEY_FILE:-${CREWDAY_DATA_DIR:-/data}/root_key}"
  if [ ! -s "$key_file" ]; then
    umask 077
    mkdir -p "$(dirname "$key_file")"
    python - <<'PY' > "$key_file"
import secrets

print(secrets.token_urlsafe(48))
PY
  fi

  CREWDAY_ROOT_KEY="$(cat "$key_file")"
  export CREWDAY_ROOT_KEY
}

case "$cmd" in
  serve)
    ensure_root_key
    alembic upgrade head
    exec python -m uvicorn app.main:create_app --factory \
      --host "${CREWDAY_BIND_HOST:-0.0.0.0}" \
      --port "${CREWDAY_BIND_PORT:-8000}" \
      "$@"
    ;;
  worker)
    ensure_root_key
    alembic upgrade head
    exec python -m app.worker "$@"
    ;;
  admin)
    exec crewday admin "$@"
    ;;
  crewday)
    exec crewday "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac
