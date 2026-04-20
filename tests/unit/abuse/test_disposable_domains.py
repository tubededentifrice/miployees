"""CI gate: the bundled disposable-domain blocklist must be fresh.

Spec §15 "Self-serve abuse mitigations" — *"if the in-repo dataset is
more than 30 days old (comment date vs. build date), CI fails the
build"*. Until the `refresh-disposable-domains.yml` workflow lands
(see the follow-up Beads task filed by cd-7huk), this unit test is
the enforcement seam: any test invocation on a host with a stale
list fails the build.

The first line of ``app/abuse/data/disposable_domains.txt`` carries
the machine-read pin in the exact format ``# generated YYYY-MM-DD``.
This test parses that token and asserts the file is younger than
:data:`_MAX_AGE_DAYS` days.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from importlib.resources import files as pkg_files
from pathlib import Path

import pytest

# Spec §15 pins the staleness gate at 30 days; change the number here
# only if the spec changes, so the test fails loudly on drift.
_MAX_AGE_DAYS = 30

# One fixed shape for the pin. Matches exactly the leading comment the
# CI refresh job will write out — no trailing context, one space,
# ISO-format date. Anchored at start so a stray ``# generated …``
# further down the file can't accidentally pass.
_PIN_RE = re.compile(r"^# generated (\d{4}-\d{2}-\d{2})\s*$")


def _bundled_path() -> Path:
    """Return the filesystem path of the bundled blocklist.

    Goes through :func:`importlib.resources.files` so the lookup
    stays correct whether :mod:`app.abuse` lives on disk or in a
    packaged wheel.
    """
    return Path(str(pkg_files("app.abuse").joinpath("data", "disposable_domains.txt")))


def _parse_generated_date(path: Path) -> datetime:
    """Return the ``# generated YYYY-MM-DD`` pin, as an aware UTC datetime.

    Raises :class:`AssertionError` if the first line doesn't match
    :data:`_PIN_RE` — the refresh job MUST write the pin in this
    shape so the CI gate is trivially parseable. A zero-byte file
    (e.g. the refresh job truncated the blocklist mid-write) surfaces
    as the same :class:`AssertionError` rather than an opaque
    :class:`IndexError`, so the operator sees the canonical "first
    line is not in … form" error regardless of how the pin went
    missing.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    first_line = lines[0] if lines else ""
    match = _PIN_RE.match(first_line)
    assert match is not None, (
        f"first line of {path} is not in '# generated YYYY-MM-DD' form: {first_line!r}"
    )
    return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)


class TestFreshnessGate:
    """The pin exists, parses, and is within the 30-day budget."""

    def test_blocklist_file_exists(self) -> None:
        path = _bundled_path()
        assert path.is_file(), f"bundled disposable-domain list missing: {path}"

    def test_first_line_has_generated_pin(self) -> None:
        """The leading comment is the canonical freshness pin."""
        _parse_generated_date(_bundled_path())  # raises on malformed pin

    def test_blocklist_is_not_stale(self) -> None:
        """Spec §15: must be <= 30 days old.

        If this fires, regenerate the list and bump the pin date. The
        refresh workflow (tracked separately — see Beads follow-up)
        will do this automatically once it lands; until then this
        test is the prompt.
        """
        generated_at = _parse_generated_date(_bundled_path())
        # Compare against "now" in aware UTC so the delta is a plain
        # :class:`timedelta`. A future-dated pin (accidentally bumped
        # ahead of today) is also rejected — a negative age is wrong
        # just as clearly as an over-age one.
        now = datetime.now(UTC)
        age = now - generated_at
        assert timedelta(days=0) <= age <= timedelta(days=_MAX_AGE_DAYS), (
            f"disposable-domain list is {age.days} days old "
            f"(generated {generated_at.date().isoformat()}, now "
            f"{now.date().isoformat()}); max allowed {_MAX_AGE_DAYS} days. "
            "Regenerate via refresh-disposable-domains.yml or manually "
            "bump the '# generated YYYY-MM-DD' pin after refreshing."
        )


class TestPinParser:
    """Pin-parser edge cases — reject anything that isn't the canonical shape."""

    @pytest.mark.parametrize(
        "bad_first_line",
        [
            "# generated",  # missing date
            "# generated 2026/04/20",  # wrong separator
            "# generated 2026-04",  # truncated
            "# GENERATED 2026-04-20",  # wrong case
            "# refreshed 2026-04-20",  # wrong keyword
            " # generated 2026-04-20",  # leading whitespace
            "",  # blank first line
        ],
    )
    def test_parser_rejects_malformed_pin(
        self, tmp_path: Path, bad_first_line: str
    ) -> None:
        """An operator who writes the pin wrong must get a loud failure,
        not a silent pass-through where the CI gate would never trip."""
        bad = tmp_path / "blocklist.txt"
        bad.write_text(f"{bad_first_line}\nexample.com\n", encoding="utf-8")
        with pytest.raises(AssertionError):
            _parse_generated_date(bad)

    def test_parser_accepts_canonical_pin(self, tmp_path: Path) -> None:
        good = tmp_path / "blocklist.txt"
        good.write_text("# generated 2026-04-20\nexample.com\n", encoding="utf-8")
        dt = _parse_generated_date(good)
        assert dt == datetime(2026, 4, 20, tzinfo=UTC)

    def test_parser_rejects_zero_byte_file(self, tmp_path: Path) -> None:
        """A truncated / zero-byte blocklist must surface as the same
        ``AssertionError`` the other malformed cases produce — not an
        opaque ``IndexError`` from ``splitlines()[0]``. Guards against a
        refresh-job mid-write truncation."""
        empty = tmp_path / "blocklist.txt"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(AssertionError):
            _parse_generated_date(empty)
