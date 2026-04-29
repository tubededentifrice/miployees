from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from starlette.datastructures import Headers, QueryParams
from starlette.requests import Request

from app.i18n import DEFAULT_LOCALE, PSEUDO_LOCALE, get_locale, resolve_locale, t


def test_t_resolves_en_us_value() -> None:
    assert t("login.title") == "Sign in to crew.day"


def test_t_interpolates_named_params() -> None:
    assert t("notification.task_assigned", task_title="Room 3") == (
        "Task assigned: Room 3"
    )


def test_missing_key_raises_when_strict() -> None:
    with pytest.raises(KeyError):
        t("missing.key", strict=True)


def test_missing_key_falls_back_to_key_when_not_strict() -> None:
    assert t("missing.key", strict=False) == "missing.key"


def test_pseudolocale_transforms_catalog_value() -> None:
    out = t("login.title", locale=PSEUDO_LOCALE)
    assert out.startswith("[!! ")
    assert out.endswith(" !!]")
    assert "Sígn" in out
    assert len(out) > len("Sign in to crew.day")


@pytest.mark.parametrize(
    (
        "query_locale",
        "preferred_locale",
        "user_languages",
        "accept_language",
        "workspace_default",
        "expected",
    ),
    [
        (PSEUDO_LOCALE, "fr-FR", (), None, None, PSEUDO_LOCALE),
        (None, "fr-FR", (), "es-MX", None, "fr-FR"),
        (None, None, ("es-MX",), "fr-FR", None, "es-MX"),
        (None, None, (), "de-DE;q=0.5, fr-FR;q=0.9", None, "fr-FR"),
        (None, None, (), "ja-JP, es-MX;q=0.8", None, "es-MX"),
        (None, None, (), None, "pt-BR", "pt-BR"),
        (None, None, (), None, "en", DEFAULT_LOCALE),
        (None, None, (), None, None, DEFAULT_LOCALE),
    ],
)
def test_resolve_locale_precedence(
    query_locale: str | None,
    preferred_locale: str | None,
    user_languages: tuple[str, ...],
    accept_language: str | None,
    workspace_default: str | None,
    expected: str,
) -> None:
    assert (
        resolve_locale(
            query_locale=query_locale,
            preferred_locale=preferred_locale,
            user_languages=user_languages,
            accept_language=accept_language,
            workspace_default=workspace_default,
        )
        == expected
    )


def test_get_locale_reads_request_state_and_headers() -> None:
    request = SimpleNamespace(
        query_params=QueryParams(""),
        headers=Headers({"accept-language": "fr-CA, en-US;q=0.7"}),
        state=SimpleNamespace(
            user=SimpleNamespace(locale=None, languages=()),
            workspace=SimpleNamespace(default_locale="es-MX"),
        ),
    )

    assert get_locale(cast(Request, request)) == "fr-CA"
