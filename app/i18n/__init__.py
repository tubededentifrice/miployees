"""Internationalization seam for server-rendered copy and formatting."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

from babel.dates import format_date as _babel_format_date
from babel.messages import pofile
from babel.numbers import format_currency as _babel_format_currency
from babel.numbers import format_decimal
from jinja2 import Environment
from starlette.requests import Request

from app.util.locales import is_valid_locale, normalise_locale

__all__ = [
    "DEFAULT_LOCALE",
    "LOCALIZED_ENUMS",
    "PSEUDO_LOCALE",
    "activate_locale",
    "format_currency",
    "format_date",
    "format_number",
    "get_locale",
    "install_jinja_i18n",
    "resolve_locale",
    "t",
]

DEFAULT_LOCALE = "en-US"
PSEUDO_LOCALE = "qps-ploc"
_CATALOG_DOMAIN = "messages"
_CATALOG_ROOT = Path(__file__).resolve().parent / "locales"
_TRANSLATION_LOCALES = frozenset({DEFAULT_LOCALE, PSEUDO_LOCALE})
_ACTIVE_LOCALE: ContextVar[str | None] = ContextVar("crewday_i18n_locale", default=None)


class _JinjaI18nEnvironment(Protocol):
    def install_gettext_callables(
        self,
        gettext: Callable[[str], str],
        ngettext: Callable[[str, str, int], str],
        newstyle: bool = False,
    ) -> None: ...


LOCALIZED_ENUMS: Mapping[str, Mapping[str, str]] = {
    "task.status": {
        "scheduled": "task.status.scheduled",
        "in_progress": "task.status.in_progress",
        "done": "task.status.done",
        "cancelled": "task.status.cancelled",
    },
    "approval.status": {
        "pending": "approval.status.pending",
        "approved": "approval.status.approved",
        "rejected": "approval.status.rejected",
    },
}


def get_locale(request: Request) -> str:
    """Resolve the UI locale for a request.

    The resolver is intentionally tolerant about request state shape:
    current code exposes ``User.locale`` while the spec names the
    future field ``preferred_locale`` and a later user-language array.
    """
    query_locale = request.query_params.get("locale")
    user = getattr(request.state, "user", None)
    workspace = getattr(request.state, "workspace", None)
    return resolve_locale(
        query_locale=query_locale,
        preferred_locale=_first_attr(user, ("preferred_locale", "locale")),
        user_languages=_iter_attr(user, "languages"),
        accept_language=request.headers.get("accept-language"),
        workspace_default=_first_attr(workspace, ("default_locale", "locale")),
    )


def resolve_locale(
    *,
    query_locale: str | None = None,
    preferred_locale: str | None = None,
    user_languages: Iterable[str] = (),
    accept_language: str | None = None,
    workspace_default: str | None = None,
) -> str:
    """Resolve UI locale by precedence.

    Order: explicit pseudo-locale query override, user preference,
    first user language, ``Accept-Language``, workspace default,
    ``en-US``.
    """
    if _normalise_candidate(query_locale) == PSEUDO_LOCALE:
        return PSEUDO_LOCALE

    for candidate in (
        preferred_locale,
        *tuple(user_languages),
        *_accept_language_candidates(accept_language),
        workspace_default,
        DEFAULT_LOCALE,
    ):
        resolved = _normalise_candidate(candidate)
        if resolved is not None:
            return resolved
    return DEFAULT_LOCALE


def t(
    key: str,
    *,
    locale: str | None = None,
    strict: bool | None = None,
    **params: object,
) -> str:
    """Translate ``key`` and interpolate named params.

    Missing keys raise by default in dev and fall back to the key in
    prod. ``strict`` lets tests and callers pin the behavior directly.
    """
    effective_locale = _normalise_candidate(locale) or DEFAULT_LOCALE
    catalog = _load_catalog(_catalog_locale(effective_locale))
    template = catalog.get(key)
    if template is None:
        template = _load_catalog(DEFAULT_LOCALE).get(key)
    if template is None:
        if strict if strict is not None else _dev_missing_keys_are_strict():
            raise KeyError(key)
        return key
    rendered = template.format(**params)
    if effective_locale == PSEUDO_LOCALE:
        return _pseudolocalize(rendered)
    return rendered


@contextmanager
def activate_locale(locale: str | None) -> Iterator[None]:
    """Make ``locale`` the active translation locale for this context."""
    token = _ACTIVE_LOCALE.set(_normalise_candidate(locale) or DEFAULT_LOCALE)
    try:
        yield
    finally:
        _ACTIVE_LOCALE.reset(token)


def format_date(
    value: date | datetime,
    *,
    locale: str | None = None,
    format: str = "medium",
) -> str:
    """Locale-aware date formatting backed by Babel."""
    return _babel_format_date(value, format=format, locale=_babel_locale(locale))


def format_number(
    value: Decimal | int | float,
    *,
    locale: str | None = None,
) -> str:
    """Locale-aware decimal formatting backed by Babel."""
    return str(format_decimal(value, locale=_babel_locale(locale)))


def format_currency(
    value: Decimal | int | float,
    currency: str,
    *,
    locale: str | None = None,
) -> str:
    """Locale-aware currency formatting backed by Babel."""
    return str(_babel_format_currency(value, currency, locale=_babel_locale(locale)))


def install_jinja_i18n(env: Environment, *, locale: str | None = None) -> None:
    """Install Jinja's i18n extension against this catalog seam."""
    if "jinja2.ext.InternationalizationExtension" not in env.extensions:
        env.add_extension("jinja2.ext.i18n")
    cast(_JinjaI18nEnvironment, env).install_gettext_callables(
        lambda message: t(message, locale=_active_or_installed_locale(locale)),
        lambda singular, plural, n: t(
            singular if n == 1 else plural,
            locale=_active_or_installed_locale(locale),
        ),
        newstyle=False,
    )


def _active_or_installed_locale(locale: str | None) -> str | None:
    return locale if locale is not None else _ACTIVE_LOCALE.get()


def _first_attr(obj: object | None, names: tuple[str, ...]) -> str | None:
    if obj is None:
        return None
    for name in names:
        value = getattr(obj, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _iter_attr(obj: object | None, name: str) -> tuple[str, ...]:
    if obj is None:
        return ()
    value = getattr(obj, name, ())
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Iterable):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _normalise_candidate(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped == PSEUDO_LOCALE:
        return PSEUDO_LOCALE
    normalised = normalise_locale(stripped)
    if normalised == "en":
        return DEFAULT_LOCALE
    if is_valid_locale(normalised):
        return normalised
    return None


def _accept_language_candidates(header: str | None) -> tuple[str, ...]:
    if not header:
        return ()
    parsed: list[tuple[float, int, str]] = []
    for order, part in enumerate(header.split(",")):
        token, *params = part.strip().split(";")
        if not token or token == "*":
            continue
        q = 1.0
        for param in params:
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q > 0:
            parsed.append((q, order, token))
    parsed.sort(key=lambda item: (-item[0], item[1]))
    return tuple(token for _, _, token in parsed)


def _catalog_locale(locale: str) -> str:
    return locale if locale in _TRANSLATION_LOCALES else DEFAULT_LOCALE


def _babel_locale(locale: str | None) -> str:
    effective = _normalise_candidate(locale) or DEFAULT_LOCALE
    if effective == PSEUDO_LOCALE:
        effective = DEFAULT_LOCALE
    return effective.replace("-", "_")


@lru_cache(maxsize=16)
def _load_catalog(locale: str) -> dict[str, str]:
    po_path = _CATALOG_ROOT / locale / "LC_MESSAGES" / f"{_CATALOG_DOMAIN}.po"
    if not po_path.exists():
        return {}
    with po_path.open("r", encoding="utf-8") as fp:
        catalog = pofile.read_po(fp, locale=locale.replace("-", "_"))
    return {
        message.id: message.string
        for message in catalog
        if (
            isinstance(message.id, str)
            and message.id
            and isinstance(message.string, str)
            and message.string
        )
    }


def _dev_missing_keys_are_strict() -> bool:
    return os.environ.get("CREWDAY_PROFILE") == "dev"


_PSEUDO_MAP = str.maketrans(
    {
        "A": "Å",
        "E": "É",
        "I": "Í",
        "O": "Ö",
        "U": "Û",
        "a": "á",
        "e": "é",
        "i": "í",
        "o": "ö",
        "u": "û",
        "y": "ý",
    }
)


def _pseudolocalize(value: str) -> str:
    inflated = value.translate(_PSEUDO_MAP)
    extra = max(1, int(len(inflated) * 0.3))
    return f"[!! {inflated}{'~' * extra} !!]"
