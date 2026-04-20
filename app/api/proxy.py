"""Dev-profile HTTP proxy to a running Vite dev server.

When ``settings.profile == "dev"`` :func:`app.main.create_app` mounts a
catch-all route that forwards non-API GETs to ``settings.vite_dev_url``
so the Vite HMR loop (tsx → browser refresh) keeps working while an
engineer edits the SPA under ``app/web/src/``. In production
(``profile == "prod"``) this module is not imported — the hot path
stays free of ``httpx``.

Scope for v1 (cd-q1be):

* HTTP forwarding for the documented Vite paths (``/``, ``/@vite/*``,
  ``/src/*``, ``/node_modules/*``, ``/favicon.ico``, everything else
  the SPA references from the dev server). The upstream body is
  buffered (``httpx`` ``.content``) before being handed to Starlette —
  Vite dev modules are small (per-file < a few hundred KB) so the
  simpler shape wins; true chunk-level streaming for large
  source-maps is a v2 follow-up.
* API routes are **not** proxied — they land on the real FastAPI
  routers via the registration-order precedence in
  :func:`app.main.create_app` and the :func:`app.main._is_api_path`
  guard inside the proxy handler (belt + braces).
* Hop-by-hop headers (``connection``, ``keep-alive``, ``te``,
  ``trailers``, ``transfer-encoding``, ``upgrade``, ``host``,
  ``content-length``) are stripped on both legs — Starlette will
  re-apply its own framing.

Not yet implemented:

* WebSocket upgrade for HMR's ``/@vite/client`` socket. Without the
  WS, Vite's HMR client logs a connection error and **hot updates on
  save do not happen** — a manual browser reload (F5) does pick up
  the latest modules through this HTTP proxy, so the dev loop is
  usable but slower. Full WS upgrade is tracked as Beads ``cd-354g``;
  until then, open Vite directly on ``127.0.0.1:5173`` in the browser
  if you need live HMR.

See ``docs/specs/14-web-frontend.md`` §"Serving the SPA",
``docs/specs/16-deployment-operations.md`` §"FastAPI static mount",
Beads ``cd-q1be``.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response, StreamingResponse

__all__ = ["register_vite_proxy"]

_log = logging.getLogger(__name__)

# Hop-by-hop headers per RFC 7230 §6.1 plus ``host`` / ``content-length``
# (Starlette / httpx re-synthesise these from the actual transport). We
# strip both on the way out (client → upstream) and on the way back
# (upstream → client) so forwarding never leaks framing state from one
# connection into the other.
_HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "host",
        "content-length",
    }
)

# Request read timeout. Vite dev servers are local (loopback by
# default) and return quickly for every file they serve; 30s is long
# enough to cover a cold-cache source-map on a slow laptop without
# letting a genuinely wedged upstream hang the browser forever.
_UPSTREAM_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0)


def _filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop + host headers before forwarding to Vite."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Strip hop-by-hop + content-length headers from the Vite response.

    We drop ``content-length`` along with the hop-by-hop set because
    Starlette's :class:`StreamingResponse` sets ``transfer-encoding:
    chunked`` itself; echoing the upstream length would let the two
    disagree on wire framing.
    """
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }


def register_vite_proxy(app: FastAPI, *, vite_dev_url: str) -> None:
    """Install the dev-profile Vite catch-all on ``app``.

    Call from :func:`app.main.create_app` only when
    ``settings.profile == "dev"``; never in prod. API routes must have
    already been registered so FastAPI's in-order route match lands
    ``/api/*`` / ``/w/{slug}/api/*`` on the real handlers; the
    in-handler :func:`_is_api_path` check defends against a future
    registration-order slip.
    """
    # Local import so the prod hot path never pays for the function-
    # level symbol — keeps ``app.main`` free of a transitive dep on a
    # dev-only seam.
    from app.main import _is_api_path

    # One long-lived AsyncClient per FastAPI app instance. Pooling +
    # HTTP/1.1 keep-alive matter here: Vite serves dozens of small
    # ``.ts`` modules per page-load and a fresh TCP handshake for each
    # would visibly slow the dev loop. Stashed on ``app.state`` so
    # tests can swap a :class:`httpx.MockTransport`-backed client in
    # without reaching into the handler's closure.
    app.state.vite_client = httpx.AsyncClient(
        base_url=vite_dev_url.rstrip("/"),
        timeout=_UPSTREAM_TIMEOUT,
        follow_redirects=False,
    )
    app.state.vite_dev_url = vite_dev_url
    # Lifecycle note: the :class:`httpx.AsyncClient` is not explicitly
    # closed — the factory has no lifespan hook yet (TODO cd-ika7 ties
    # one in for the authz cache). The pooled sockets are reclaimed on
    # process exit; a proper ``lifespan`` owner will fold this in when
    # the WebSocket HMR upgrade (cd-354g) lands alongside its own
    # shutdown coordination.

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def vite_proxy(full_path: str, request: Request) -> Response:
        """Stream the Vite dev server's response back to the caller.

        ``full_path`` is empty on the root (``/``); we forward that as
        ``/`` so Vite serves its ``index.html``. Query strings are
        preserved verbatim — Vite uses ``?t=<timestamp>`` cache busters
        + ``?v=<hash>`` invalidation and every one of them matters.
        """
        path = "/" + full_path
        if _is_api_path(path):
            # Defensive guard: registration order should have already
            # peeled these off, but a JSON envelope is the correct
            # shape for agent / CLI callers in any case.
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": None},
            )

        query = request.url.query
        upstream_path = path + (f"?{query}" if query else "")

        # Read the client via ``app.state`` on every request so tests
        # can swap it after factory construction. Production traffic
        # pays one attribute lookup per request — trivial next to the
        # network hop we're about to do.
        client: httpx.AsyncClient = request.app.state.vite_client
        upstream_base: str = request.app.state.vite_dev_url

        try:
            upstream = await client.get(
                upstream_path,
                headers=_filter_request_headers(dict(request.headers)),
            )
        except httpx.RequestError as exc:
            _log.warning(
                "vite proxy: upstream unreachable",
                extra={
                    # Underscores — dot-separated triples are masked as
                    # JWTs by the redaction filter (see app.util.logging).
                    "event": "spa_vite_proxy_failed",
                    "path": path,
                    "error": str(exc),
                },
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "vite_unreachable",
                    "detail": f"Vite dev server at {upstream_base} did not respond",
                },
            )

        return StreamingResponse(
            content=iter([upstream.content]),
            status_code=upstream.status_code,
            headers=_filter_response_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )
