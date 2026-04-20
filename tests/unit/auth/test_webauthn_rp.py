"""Tests for :mod:`app.auth.webauthn` — RP config + policy.

Covers the acceptance criteria on cd-gox:

* :class:`RelyingParty` / :class:`WebAuthnPolicy` are frozen + slotted.
* :func:`make_relying_party` derives ``rp_id`` from the origin
  hostname, strips the trailing slash, honours the
  ``CREWDAY_WEBAUTHN_RP_ID`` override, and refuses mismatches.
* :func:`policy` returns the spec's values verbatim.
* No file under ``app/`` imports the upstream ``webauthn`` package
  except ``app/auth/webauthn.py`` — enforced via an AST walk so a
  future drive-by ``from webauthn import …`` fails CI loudly.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.auth.webauthn import (
    RelyingParty,
    RelyingPartyMisconfigured,
    make_relying_party,
    policy,
)
from app.config import Settings, get_settings

if TYPE_CHECKING:
    from pytest import MonkeyPatch


_CREWDAY_VARS: tuple[str, ...] = tuple(
    f"CREWDAY_{name.upper()}" for name in Settings.model_fields
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate each test from host env + repo-root ``.env``.

    Mirrors ``tests/unit/test_config.py`` — strip every ``CREWDAY_*``
    var, ``chdir`` into a temp dir so pydantic-settings can't pick up a
    stray ``.env``, and clear ``get_settings``' cache on both entry
    and exit.
    """
    for name in _CREWDAY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _settings(**overrides: str) -> Settings:
    """Build a :class:`Settings` with ``CREWDAY_DATABASE_URL`` defaulted."""
    env = {"database_url": "sqlite:///:memory:", **overrides}
    return Settings(**env)


class TestRelyingPartyShape:
    def test_frozen(self) -> None:
        rp = RelyingParty(
            rp_id="example.com",
            rp_name="crew.day",
            origin="https://example.com",
            allowed_origins=("https://example.com",),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rp.rp_id = "other.example.com"  # type: ignore[misc]

    def test_slotted(self) -> None:
        rp = RelyingParty(
            rp_id="example.com",
            rp_name="crew.day",
            origin="https://example.com",
            allowed_origins=("https://example.com",),
        )
        # Slotted dataclasses have no ``__dict__``; attribute writes go
        # through descriptors bound to the slots. ``__slots__`` is a
        # dataclass-emitted attribute we can assert on directly.
        assert not hasattr(rp, "__dict__")
        assert set(RelyingParty.__slots__) == {
            "rp_id",
            "rp_name",
            "origin",
            "allowed_origins",
        }


class TestWebAuthnPolicyShape:
    def test_frozen(self) -> None:
        pol = policy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pol.user_verification = "preferred"  # type: ignore[misc]

    def test_slotted(self) -> None:
        pol = policy()
        assert not hasattr(pol, "__dict__")

    def test_spec_values_exact(self) -> None:
        """Values from §03 'WebAuthn specifics' verbatim."""
        pol = policy()
        # UserVerificationRequirement / AttestationConveyancePreference
        # are str-Enum subclasses, so string equality is fine.
        assert pol.user_verification == "required"
        assert pol.attestation == "none"
        assert pol.attachment_preferred == "platform"
        assert pol.attachment_allow_cross_platform is True
        assert pol.resident_keys == "preferred"
        assert pol.pub_key_algs == (-7, -257)
        assert pol.timeout_ms == 60_000

    def test_fresh_instance_each_call(self) -> None:
        """Two calls yield distinct objects — frozen, but not a singleton."""
        first = policy()
        second = policy()
        assert first is not second
        # Equal by value, since the fields are identical.
        assert first == second


class TestMakeRelyingParty:
    def test_derives_rp_id_from_public_url(self) -> None:
        rp = make_relying_party(_settings(public_url="https://app.example.com"))
        assert rp.rp_id == "app.example.com"
        assert rp.origin == "https://app.example.com"
        assert rp.allowed_origins == ("https://app.example.com",)
        assert rp.rp_name == "crew.day"

    def test_trailing_slash_stripped(self) -> None:
        rp = make_relying_party(_settings(public_url="https://app.example.com/"))
        assert rp.origin == "https://app.example.com"
        assert rp.rp_id == "app.example.com"

    def test_multiple_trailing_slashes_stripped(self) -> None:
        """``rstrip("/")`` removes every trailing ``/`` — cheap belt-and-braces."""
        rp = make_relying_party(_settings(public_url="https://app.example.com///"))
        assert rp.origin == "https://app.example.com"

    def test_https_host_with_port(self) -> None:
        """Port in the origin is fine; rp_id is still the bare hostname."""
        rp = make_relying_party(_settings(public_url="https://example.com:8443"))
        assert rp.rp_id == "example.com"
        assert rp.origin == "https://example.com:8443"

    def test_dev_fallback_when_public_url_unset(self) -> None:
        """No ``CREWDAY_PUBLIC_URL`` → synthesise from bind host/port."""
        rp = make_relying_party(_settings())
        assert rp.origin == "http://127.0.0.1:8000"
        assert rp.rp_id == "127.0.0.1"

    def test_uppercase_host_is_canonicalised(self) -> None:
        """Browsers report ``clientDataJSON.origin`` with a lowercased host.

        py_webauthn compares ``expected_origin`` byte-for-byte, so an
        uppercase host in ``CREWDAY_PUBLIC_URL`` would make every
        ceremony fail silently. We normalise at boot instead.
        """
        rp = make_relying_party(_settings(public_url="https://APP.EXAMPLE.COM"))
        assert rp.origin == "https://app.example.com"
        assert rp.rp_id == "app.example.com"

    def test_uppercase_scheme_is_canonicalised(self) -> None:
        rp = make_relying_party(_settings(public_url="HTTPS://app.example.com"))
        assert rp.origin == "https://app.example.com"

    def test_path_on_public_url_is_stripped(self) -> None:
        """Origins never carry a path; a pasted ``/`` or ``/app`` is trimmed."""
        rp = make_relying_party(_settings(public_url="https://app.example.com/app"))
        assert rp.origin == "https://app.example.com"

    def test_query_and_fragment_stripped(self) -> None:
        rp = make_relying_party(
            _settings(public_url="https://app.example.com/?x=1#frag")
        )
        assert rp.origin == "https://app.example.com"

    def test_userinfo_stripped(self) -> None:
        """``user:pass@`` in the URL would never appear in a browser origin."""
        rp = make_relying_party(
            _settings(public_url="https://user:pass@app.example.com")
        )
        assert rp.origin == "https://app.example.com"
        assert rp.rp_id == "app.example.com"

    def test_port_preserved(self) -> None:
        """Explicit port is preserved in the canonical origin."""
        rp = make_relying_party(_settings(public_url="https://app.example.com:8443"))
        assert rp.origin == "https://app.example.com:8443"
        assert rp.rp_id == "app.example.com"

    def test_schemeless_url_rejected(self) -> None:
        """A schemeless authority like ``//example.com`` fails loudly."""
        with pytest.raises(RelyingPartyMisconfigured):
            make_relying_party(_settings(public_url="//example.com"))

    def test_unsupported_scheme_rejected(self) -> None:
        """Only ``http``/``https`` are valid WebAuthn origins."""
        with pytest.raises(RelyingPartyMisconfigured) as excinfo:
            make_relying_party(_settings(public_url="ftp://example.com"))
        assert "http" in str(excinfo.value).lower()

    def test_invalid_port_rejected(self) -> None:
        """A non-numeric port surfaces as a clean boot error, not a crash."""
        with pytest.raises(RelyingPartyMisconfigured) as excinfo:
            make_relying_party(_settings(public_url="https://example.com:abc"))
        assert "port" in str(excinfo.value).lower()

    def test_dev_fallback_uses_http_for_non_loopback_bind(self) -> None:
        """Tailscale-style bind falls back to ``http://`` (no TLS locally)."""
        rp = make_relying_party(_settings(bind_host="100.72.198.118", bind_port="9000"))
        assert rp.origin == "http://100.72.198.118:9000"
        assert rp.rp_id == "100.72.198.118"

    def test_override_matching_hostname(self) -> None:
        """``CREWDAY_WEBAUTHN_RP_ID`` override equal to the origin host is fine."""
        rp = make_relying_party(
            _settings(
                public_url="https://app.example.com",
                webauthn_rp_id="app.example.com",
            )
        )
        assert rp.rp_id == "app.example.com"

    def test_override_case_insensitive_match(self) -> None:
        """DNS labels are case-insensitive — an uppercase override must match."""
        rp = make_relying_party(
            _settings(
                public_url="https://app.example.com",
                webauthn_rp_id="EXAMPLE.COM",
            )
        )
        # Stored lowercased — py_webauthn does byte-exact expected_rp_id
        # comparison against the lowercased host the browser reports.
        assert rp.rp_id == "example.com"

    def test_override_with_parent_suffix(self) -> None:
        """Override may be a registrable parent suffix of the origin host."""
        rp = make_relying_party(
            _settings(
                public_url="https://app.example.com",
                webauthn_rp_id="example.com",
            )
        )
        assert rp.rp_id == "example.com"
        assert rp.origin == "https://app.example.com"

    def test_override_not_a_suffix_raises(self) -> None:
        with pytest.raises(RelyingPartyMisconfigured) as excinfo:
            make_relying_party(
                _settings(
                    public_url="https://app.example.com",
                    webauthn_rp_id="other.com",
                )
            )
        msg = str(excinfo.value)
        assert "other.com" in msg
        assert "app.example.com" in msg

    def test_override_substring_not_suffix_raises(self) -> None:
        """A substring that isn't a dot-bounded suffix must be rejected.

        ``example.com`` is a substring of ``myexample.com`` but not a
        registrable suffix — a bare ``endswith`` check would incorrectly
        accept it. The validator uses a dot-prefixed match to rule it out.
        """
        with pytest.raises(RelyingPartyMisconfigured):
            make_relying_party(
                _settings(
                    public_url="https://myexample.com",
                    webauthn_rp_id="example.com",
                )
            )

    def test_public_url_without_scheme_raises(self) -> None:
        """Garbage ``public_url`` (no scheme) fails fast, doesn't silently fall back."""
        with pytest.raises(RelyingPartyMisconfigured) as excinfo:
            make_relying_party(_settings(public_url="not-a-url"))
        # Rejected for scheme, not hostname — both are misconfigurations
        # but scheme is the cheaper check and we fail on that first.
        assert "http" in str(excinfo.value).lower()

    def test_public_url_scheme_only_raises(self) -> None:
        """``https:///`` has a scheme but no host — rejected with a host error."""
        with pytest.raises(RelyingPartyMisconfigured) as excinfo:
            make_relying_party(_settings(public_url="https:///"))
        assert "hostname" in str(excinfo.value).lower()

    def test_defaults_to_get_settings_when_no_arg(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """Calling without a ``Settings`` reads the process-wide cache."""
        monkeypatch.setenv("CREWDAY_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("CREWDAY_PUBLIC_URL", "https://app.example.com")
        rp = make_relying_party()
        assert rp.rp_id == "app.example.com"
        assert rp.origin == "https://app.example.com"


class TestWebauthnImportBoundary:
    """Only ``app/auth/webauthn.py`` may import the upstream ``webauthn``
    package. A second import would fracture the seam — the point of the
    module is to funnel every passkey call through one file. An AST
    walk over every ``.py`` under ``app/`` catches both
    ``import webauthn`` and ``from webauthn[...] import …``.
    """

    @staticmethod
    def _iter_app_python_files() -> Iterator[Path]:
        app_root = Path(__file__).resolve().parents[3] / "app"
        assert app_root.is_dir(), f"expected {app_root} to exist"
        for path in app_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path

    @staticmethod
    def _imports_webauthn(tree: ast.AST) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "webauthn" or alias.name.startswith("webauthn."):
                        return True
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                if node.module == "webauthn" or node.module.startswith("webauthn."):
                    return True
        return False

    def test_only_auth_webauthn_imports_webauthn(self) -> None:
        allowed = (
            Path(__file__).resolve().parents[3] / "app" / "auth" / "webauthn.py"
        ).resolve()
        violations: list[Path] = []
        for path in self._iter_app_python_files():
            if path.resolve() == allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if self._imports_webauthn(tree):
                violations.append(path)
        assert not violations, (
            "Only app/auth/webauthn.py may import `webauthn`; "
            f"offending files: {[str(p) for p in violations]}"
        )
