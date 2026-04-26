"""Hand-written ``crewday auth login`` override.

Spec ``docs/specs/13-cli.md`` §"Config" + §"crewday auth": ``auth
login`` is the interactive flow that writes a profile into
``~/.config/crewday/config.toml`` and pings ``GET /healthz`` against
the chosen ``base_url`` so the user gets immediate feedback that the
profile is wired correctly. There is no API analogue (the underlying
HTTP surface is just the bare-host ``/healthz`` probe), so the
``covers=[]`` claim leaves nothing for the parity gate to mark.

Atomicity: the new TOML is written to a sibling temp file in the same
directory and renamed onto the final location. ``os.replace`` is
atomic on POSIX and Windows so a crash mid-write never leaves a
partial config behind. Existing profiles outside the one being
written are preserved verbatim by reading the file via
:mod:`tomllib` first.

Token storage: by default the token is written as ``token = "<value>"``
(plain text). ``--token-env VAR`` rewrites the storage to ``token =
"env:VAR"`` so the file never carries the secret — the resolver
expands ``env:VAR`` at load time. This matches §13's "avoids storing
secrets in the config file" guidance.
"""

from __future__ import annotations

import os
import pathlib
import tomllib
from typing import Final

import click
import httpx

from crewday._main import ConfigError
from crewday._overrides import cli_override

__all__ = ["register"]


_CONFIG_DIR: Final[pathlib.Path] = pathlib.Path.home() / ".config" / "crewday"
_CONFIG_FILE: Final[pathlib.Path] = _CONFIG_DIR / "config.toml"

_DEFAULT_PROFILE_NAME: Final[str] = "default"
_DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8100"

# Probe timeout; ``auth login`` is run interactively, so a long hang
# on a wrong base URL is worse than a fast "I gave up". 10 s covers a
# legitimately-slow homelab while still being short enough that an
# operator notices the failure mode quickly.
_HEALTHZ_TIMEOUT_SECONDS: Final[float] = 10.0


def _read_existing_config(path: pathlib.Path) -> dict[str, object]:
    """Return the parsed config or an empty dict if the file is missing.

    A missing file is the common "first profile" case; we preserve any
    pre-existing profiles so writing a new one is non-destructive.
    A *corrupt* file is a hard error — silently overwriting would
    discard the operator's other profiles, which is exactly the
    failure mode atomic writes are meant to prevent in the first
    place.
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return dict(tomllib.load(fh))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"existing config at {path} is not valid TOML: {exc}. "
            "Fix or remove it before re-running 'crewday auth login'."
        ) from exc


def _format_toml(config: dict[str, object]) -> str:
    """Hand-format the config back to TOML text.

    The shape is small and well-known: a ``default_profile`` scalar
    plus one ``[profile.<name>]`` table per entry, each with 2-4 string
    keys. Reaching for a dependency (``tomli_w``) for that is overkill;
    the writer is twenty lines and round-trips through :mod:`tomllib`
    in the test suite.

    Strings are emitted with double quotes; ``\\`` and ``"`` inside a
    value are escaped per the TOML "basic string" production. Other
    control characters never appear in our values (URL, ULID/email/UUID,
    short identifiers), so the writer asserts ASCII-printable input
    rather than implementing the full escape table — the next agent
    who tries to write a tab-separated token will get a clear error.
    """
    lines: list[str] = []
    default_profile = config.get("default_profile")
    if isinstance(default_profile, str):
        lines.append(f"default_profile = {_toml_string(default_profile)}")
        lines.append("")

    profiles = config.get("profile")
    if isinstance(profiles, dict):
        # Sort for deterministic output; idempotent re-writes produce
        # byte-identical files which keeps diffs small and makes the
        # idempotency test (re-run ``auth login`` twice → same bytes)
        # trivial.
        for name in sorted(profiles):
            entry = profiles[name]
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"config 'profile.{name}' must be a table, "
                    f"got {type(entry).__name__}"
                )
            lines.append(f"[profile.{name}]")
            for key in sorted(entry):
                value = entry[key]
                if not isinstance(value, str):
                    raise ConfigError(
                        f"config 'profile.{name}.{key}' must be a string, "
                        f"got {type(value).__name__}"
                    )
                lines.append(f"{key} = {_toml_string(value)}")
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _toml_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string.

    Asserts the value is printable ASCII so we don't have to ship the
    full escape table for control characters that have no business in
    our field set. ``\\`` and ``"`` get the standard backslash escape.
    """
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ConfigError(
                f"refusing to write control character (0x{ord(ch):02X}) into "
                "config.toml; the values for profile fields must be printable "
                "ASCII"
            )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_atomic(path: pathlib.Path, contents: str) -> None:
    """Write ``contents`` to ``path`` via temp-file + rename.

    A crash partway through ``write_text`` would leave a half-written
    file; the rename closes that window — the temp file lives in the
    same directory so the rename is atomic on every supported OS. We
    chmod the temp file to 0600 before renaming so the secret-bearing
    config is never world-readable, regardless of the umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(contents, encoding="utf-8")
    # 0600: rw for the owner, nothing for anyone else. The token may
    # still leak via ``--token-env``-skipping callers; the secrets-in-
    # env story is the user's choice.
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def _ping_healthz(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Probe ``GET <base_url>/healthz``; raise :class:`ConfigError` on failure.

    The bare-host ``/healthz`` probe (no ``/w/<slug>`` prefix) is the
    auth-free liveness signal per spec §16 "Health". We don't carry an
    Authorization header so a legitimate liveness response of 401
    (which shouldn't happen on ``/healthz`` but covers a misconfigured
    badger forward-auth) gets a clean error rather than confusing
    "wrong token" noise.

    Tests inject ``transport=httpx.MockTransport(...)`` to fake the
    server; production uses httpx's default transport.
    """
    healthz_url = base_url.rstrip("/") + "/healthz"
    try:
        with httpx.Client(
            timeout=_HEALTHZ_TIMEOUT_SECONDS,
            transport=transport,
        ) as client:
            response = client.get(healthz_url)
    except httpx.TransportError as exc:
        raise ConfigError(
            f"could not reach {healthz_url}: {type(exc).__name__}: {exc}. "
            "Check the base URL and the network."
        ) from exc

    if not (200 <= response.status_code < 300):
        raise ConfigError(
            f"{healthz_url} returned HTTP {response.status_code}; expected 2xx. "
            "The base URL might be wrong, or the service is unhealthy."
        )


def _prompt_string(prompt: str, *, default: str) -> str:
    """Wrap :func:`click.prompt` to narrow the return value to ``str``.

    ``click.prompt`` is typed ``Any`` (its return type depends on the
    runtime ``type=`` argument); narrowing in one place keeps the
    callers free of repeated isinstance checks while still keeping
    ``mypy --strict`` honest.
    """
    raw = click.prompt(prompt, default=default, type=str)
    if not isinstance(raw, str):
        raise click.UsageError(f"{prompt!r} must be a string")
    return raw


def _resolve_token(
    *,
    token_arg: str | None,
    token_env: str | None,
) -> str:
    """Return the value to write into the ``token`` field.

    Three resolution paths:

    * ``--token-env VAR`` — store the literal ``"env:VAR"``, never
      the secret itself. The runtime resolver expands it at load time.
    * ``--token <value>`` — store the plain value. Discouraged but
      supported; the file is chmodded 0600 to limit the blast radius.
    * Neither — prompt interactively (``hide_input=True`` so the
      terminal never echoes the secret).

    Combining ``--token`` and ``--token-env`` is rejected at the
    boundary so the user never wonders which one wins.
    """
    if token_env is not None and token_arg is not None:
        raise click.UsageError(
            "--token and --token-env are mutually exclusive; pick one."
        )
    if token_env is not None:
        if not token_env:
            raise click.UsageError("--token-env value must not be empty.")
        return f"env:{token_env}"
    if token_arg is not None:
        if not token_arg:
            raise click.UsageError("--token value must not be empty.")
        return token_arg
    prompted = click.prompt(
        "Token (paste your API token; input is hidden)",
        hide_input=True,
        confirmation_prompt=False,
        type=str,
    )
    # ``click.prompt`` is typed as ``Any``; narrow defensively so the
    # function honours its declared ``str`` return type without a cast.
    if not isinstance(prompted, str) or not prompted:
        raise click.UsageError("token must not be empty.")
    return prompted


@click.command(name="login")
@click.option(
    "--profile",
    "profile_name",
    default=None,
    help="Profile name to write under [profile.<name>] (default: 'default').",
)
@click.option(
    "--base-url",
    "base_url",
    default=None,
    help=f"Base URL to ping for /healthz (default: '{_DEFAULT_BASE_URL}').",
)
@click.option(
    "--token",
    "token_arg",
    default=None,
    help=(
        "API token to store. Discouraged — pass --token-env VAR instead so "
        "the secret stays out of the config file."
    ),
)
@click.option(
    "--token-env",
    "token_env",
    default=None,
    help=(
        "Name of the env var that will hold the token at runtime; the file "
        "stores 'env:VAR' so the secret stays in the operator's vault."
    ),
)
def login(
    *,
    profile_name: str | None,
    base_url: str | None,
    token_arg: str | None,
    token_env: str | None,
) -> None:
    """Write or update a profile in ~/.config/crewday/config.toml.

    Walks through the four pieces of state every profile needs (name,
    base URL, token storage strategy, ping-check) and lands the result
    on disk via an atomic temp-file rename. The healthz probe runs
    *before* the write so a wrong URL does not pollute the config.

    Output is one human-readable line confirming the resolved profile
    name + base URL; the spec §13 §"Output" JSON contract does not
    apply to interactive flows.
    """
    resolved_profile = profile_name or _prompt_string(
        "Profile name",
        default=_DEFAULT_PROFILE_NAME,
    )
    if not resolved_profile:
        raise click.UsageError("profile name must not be empty.")

    resolved_base_url = base_url or _prompt_string(
        "Base URL",
        default=_DEFAULT_BASE_URL,
    )
    if not resolved_base_url:
        raise click.UsageError("base URL must not be empty.")

    token_value = _resolve_token(token_arg=token_arg, token_env=token_env)

    # Probe before write — a failing ``/healthz`` aborts cleanly with
    # exit 5 (CONFIG_ERROR) and the on-disk config stays untouched.
    _ping_healthz(resolved_base_url)

    config = _read_existing_config(_CONFIG_FILE)
    profiles_obj = config.get("profile")
    if profiles_obj is not None and not isinstance(profiles_obj, dict):
        raise ConfigError(
            f"existing config at {_CONFIG_FILE} has a non-table 'profile' key; "
            "fix or remove it before re-running 'crewday auth login'."
        )
    profiles: dict[str, object] = (
        dict(profiles_obj) if isinstance(profiles_obj, dict) else {}
    )

    profiles[resolved_profile] = {
        "base_url": resolved_base_url,
        "token": token_value,
    }
    config["profile"] = profiles
    config.setdefault("default_profile", resolved_profile)

    _write_atomic(_CONFIG_FILE, _format_toml(config))

    click.echo(
        f"Wrote profile {resolved_profile!r} → {resolved_base_url} ({_CONFIG_FILE})."
    )


# Stamp the override metadata after construction. ``covers=[]`` because
# ``auth login`` has no API analogue — there is no ``operation_id`` for
# "interactive profile setup". The parity gate sees an empty cover set
# and skips the command without flagging it as missing.
login = cli_override("auth", "login", covers=[])(login)


def register(root: click.Group) -> None:
    """Attach ``auth login`` to the root group's ``auth`` subgroup.

    The ``auth`` group is created by the codegen pipeline (it has
    generated verbs like ``whoami``, ``tokens``, ``passkey``); we
    look it up and add to it so the override sits next to its
    siblings under ``crewday auth --help``. If codegen hasn't
    registered ``auth`` yet (e.g. an aggressive exclusion list), we
    create the group ourselves so the override is still reachable.
    """
    auth_group = root.commands.get("auth")
    if auth_group is None:
        auth_group = click.Group(name="auth", help="auth commands")
        root.add_command(auth_group)
    if not isinstance(auth_group, click.Group):
        raise RuntimeError(
            "expected 'auth' to be a click.Group; cannot attach 'login' "
            "override to a leaf command."
        )
    auth_group.add_command(login)
