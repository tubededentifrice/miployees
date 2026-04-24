"""``POST`` / ``DELETE /api/v1/me/avatar`` — identity-scoped avatar surface.

Bare-host router, tenant-agnostic. The SPA's avatar editor
(``app/web/src/components/AvatarEditor.tsx``) hits this endpoint with
a ``multipart/form-data`` body containing the cropped 512x512 WebP the
client composed on canvas; the server stores the blob by its SHA-256
digest through the content-addressed :class:`Storage` port and points
``users.avatar_blob_hash`` at it. ``DELETE`` clears the pointer.

**Self-only** (§05 "Worker surface", §12 "Avatar upload"). There is no
manager override — a manager who needs to fix a team member's avatar
uses the worker's own device. The router reads the session cookie
directly (mirroring ``me.py`` / ``me_tokens.py``); no workspace
context is resolved and no grant is checked.

**Orphan GC (not here).** Replacing or clearing an avatar does NOT
delete the previous blob — a separate sweep (cd-3i5 sibling task)
reconciles ``users.avatar_blob_hash`` against ``storage.exists`` and
drops orphans. Deleting inline would race with any request that
already resolved the old signed URL; the GC sweep is safer.

**Audit rows**: every state-changing call writes one row via
:func:`write_audit`. ``POST`` emits ``identity.avatar.updated`` with
``{"before": <prior_hash>, "after": <new_hash>}``; ``DELETE`` emits
``identity.avatar.cleared`` with ``{"before": <prior_hash>}`` but
only when the prior hash was set (an already-null clear is a no-op
from an audit standpoint — we don't want to spam the log with
idempotent retries that carry no information). The context passed to
:func:`write_audit` is a synthetic :class:`WorkspaceContext` minted
by :func:`_identity_audit_ctx` — same zero-ULID workspace / ``system``
actor-kind shape every other bare-host identity surface uses
(:func:`app.auth.session._agnostic_audit_ctx`,
:func:`app.auth.magic_link._agnostic_audit_ctx`, …); the acting
user's id rides in the diff because the synthetic actor-id slot is
reserved for the ``0``-ULID sentinel. cd-rqhy tracks the follow-up
that will extract the synthetic-ctx factory to a shared helper.

**Content-type allowlist** (§12 "Avatar upload"):

* ``image/png``
* ``image/jpeg``
* ``image/webp``

``image/heic`` is deferred to a follow-up (the server decode path
isn't wired for it yet). Any other content type → ``415``.

**Size cap**: 2 MB. The task prompt pins this explicitly; §12's
broader 10 MB cap covers the editor's source image, but the server
only ever receives the already-cropped 512x512 output, so a 2 MB
envelope is comfortable for a high-quality WebP. The router enforces
the limit twice:

1. Synchronously on the ``Content-Length`` header (fail fast before
   buffering a byte).
2. Via a streaming accumulator that short-circuits once the byte
   count exceeds 2 MB + 1 — defence against a client that omits
   ``Content-Length`` or lies.

See ``docs/specs/05-employees-and-roles.md`` §"Worker surface",
``docs/specs/12-rest-api.md`` §"Avatar upload", and
``docs/specs/15-security-privacy.md`` §"Blob download authorization".
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Annotated, Final

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.storage.ports import Storage
from app.api.deps import db_session, get_storage
from app.audit import write_audit
from app.auth import session as auth_session
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "AvatarResponse",
    "build_me_avatar_router",
]


_log = logging.getLogger(__name__)

# Synthetic tenant-agnostic actor + workspace sentinels for audit
# rows emitted from this bare-host router. Same zero-ULID shape as
# :func:`app.auth.session._agnostic_audit_ctx` + the three other
# ``_agnostic_audit_ctx`` helpers in :mod:`app.auth` — identity
# mutations are not workspace-scoped, so the writer gets a synthetic
# ctx that keeps its NOT-NULL contract without pretending the row
# belongs to a specific tenant. ``_AGNOSTIC_ACTOR_ID`` keeps the
# actor-identity slot readable at a glance in the audit log; the
# acting user's real id rides in the diff payload instead. Tracked
# as cd-rqhy to extract the shared helper.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "0" * 26
_AGNOSTIC_ACTOR_ID: Final[str] = "0" * 26

# 2 MiB upload cap. The stored blob is a client-side-cropped 512x512
# WebP; 2 MiB leaves ample headroom for lossless + high-quality output
# without exposing a "stream a 10 GB payload until the FD runs out"
# amplifier. Pinned as ``2 * 1024 * 1024`` (not ``2_000_000``) so the
# boundary is unambiguous in both MiB- and decimal-MB reasoning.
_MAX_BYTES: Final[int] = 2 * 1024 * 1024

# 1-hour TTL for the signed URL. Matches the SPA's refresh cadence —
# the editor requeries ``/auth/me`` after a successful save, so the
# URL only needs to outlive the render pass that triggered it. A
# longer window would widen the replay surface on the signed link
# without any UX win.
_SIGN_TTL_SECONDS: Final[int] = 3600

# Allowlisted upload content types. ``image/heic`` is deferred — the
# server decode path is not wired for it yet, and accepting it here
# would store undecoded bytes the SPA can't render on non-Safari
# clients.
_ALLOWED_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

# Read chunk size for the streaming guard. 64 KiB matches the
# localfs storage's ``_CHUNK_SIZE`` — same page-cache sweet spot, and
# the symmetry keeps future instrumentation (byte-rate logging) honest.
_READ_CHUNK: Final[int] = 64 * 1024


_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]


class AvatarResponse(BaseModel):
    """Body returned by both ``POST`` and ``DELETE /api/v1/me/avatar``.

    Matches the SPA's expected envelope (``AvatarEditor.tsx``). The
    ``avatar_url`` is a time-limited signed URL on ``POST`` (the
    storage port emits ``/api/v1/files/<sig>?h=<hash>&e=<exp>`` for
    :class:`~app.adapters.storage.localfs.LocalFsStorage`) and
    ``null`` on ``DELETE``.
    """

    avatar_url: str | None


def _client_headers(request: Request) -> tuple[str, str]:
    """Return ``(ua, accept_language)`` for :func:`auth_session.validate`.

    Mirrors the helper in :mod:`app.api.v1.auth.me` — the session
    fingerprint gate reads both headers. Empty strings skip the gate
    (see :func:`auth_session.validate`); the SPA always sends them, so
    prod traffic exercises the full check.
    """
    return (
        request.headers.get("user-agent", ""),
        request.headers.get("accept-language", ""),
    )


def _resolve_session_user(
    session: Session,
    request: Request,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
) -> User:
    """Return the authenticated :class:`User` or raise ``HTTPException``.

    Same shape as :func:`app.api.v1.auth.me_tokens._resolve_session_user`
    but returns the hydrated :class:`User` row instead of just the id —
    the avatar router mutates the row directly, so saving a second
    ``session.get(User, user_id)`` bounce is worth the wider return
    type.
    """
    cookie_value = cookie_primary or cookie_dev
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_required"},
        )
    ua, accept_language = _client_headers(request)
    try:
        user_id = auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
        )
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        ) from exc

    # ``user`` is identity-scoped — no workspace filter to apply.
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        # Row referenced by the session was hard-deleted between
        # validate and lookup. Collapse to 401 — same shape the SPA
        # already handles on a stale session.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        )
    return user


def _read_capped(upload: UploadFile) -> bytes:
    """Return the upload's bytes, raising 413 past :data:`_MAX_BYTES`.

    Streams the body in :data:`_READ_CHUNK`-sized pulls so a client
    that lies about ``Content-Length`` (omits it, forges a small
    value) can't exhaust memory — the accumulator short-circuits once
    the running total crosses ``_MAX_BYTES`` and the HTTP response is
    413 without waiting for the upload to finish.

    The SPA's ``AvatarEditor`` composes a single cropped 512x512 WebP
    so the realistic payload is well under the cap; the guard is
    defence against a malicious / broken client.
    """
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = upload.file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"error": "avatar_too_large"},
            )
        buffer.write(chunk)
    return buffer.getvalue()


def _check_content_length(request: Request) -> None:
    """Raise 413 when the client advertises an oversized body.

    Exposed as a FastAPI dep (not an inline call) so it runs before
    the multipart body parser — otherwise FastAPI would buffer the
    entire upload into memory to populate the :class:`UploadFile`
    parameter before the handler body gets a chance to look at the
    header. Dependencies are resolved ahead of body params, so this
    dep is the first gate the router opens.

    Content-Length can be absent (chunked transfer) or lie; the
    streaming guard in :func:`_read_capped` is the authoritative
    check. This fast-path saves the buffering cost when the client
    admits to an oversized upload, which is the common well-behaved
    rejection shape.
    """
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        # Malformed Content-Length — let FastAPI / Starlette's normal
        # parsing surface the underlying error rather than translating
        # it here. A non-numeric header isn't specifically an
        # "avatar too large" condition.
        return
    if size > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": "avatar_too_large"},
        )


_ContentLengthGuard = Annotated[None, Depends(_check_content_length)]


def _identity_audit_ctx() -> WorkspaceContext:
    """Return a sentinel :class:`WorkspaceContext` for avatar audit rows.

    Mirrors :func:`app.auth.session._agnostic_audit_ctx` and the
    other four bare-host identity surfaces that mint their own copy:
    zero-ULID workspace + actor, ``actor_kind="system"``, fresh
    correlation id per request. The caller's real user id is carried
    in the ``diff`` payload (not in ``actor_id``) because the
    synthetic actor-id slot is reserved for the zero-ULID sentinel —
    every bare-host identity writer uses the same shape so dashboards
    filtering on ``actor_kind='system'`` aggregate them uniformly.
    cd-rqhy tracks the follow-up that will extract this factory to a
    shared helper and surface the acting user's id in a first-class
    column rather than a diff field.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=_AGNOSTIC_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",  # unused for system actors
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def build_me_avatar_router() -> APIRouter:
    """Return the router serving ``/api/v1/me/avatar``.

    Factory shape matches the other auth-router builders so the app
    factory's wiring seam stays uniform and tests can mount the
    endpoint against an isolated FastAPI instance (see
    :mod:`tests.unit.api.v1.auth.test_me_avatar`).
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``me`` stays for fine-grained client filtering.
    router = APIRouter(prefix="/me", tags=["identity", "me"])

    @router.post(
        "/avatar",
        response_model=AvatarResponse,
        operation_id="auth.me.avatar.set",
        summary="Upload the caller's avatar image (self-only)",
        openapi_extra={
            # Avatars are a browser-driven UX — the SPA composes the
            # cropped WebP on canvas before POSTing. There is no sane
            # CLI verb for "upload a cropped image"; hide the surface
            # so the generator does not mint one.
            "x-cli": {
                "group": "me",
                "verb": "avatar-set",
                "summary": "Set your avatar image",
                "mutates": True,
                "hidden": True,
            },
            "x-interactive-only": True,
        },
    )
    def post_me_avatar(
        request: Request,
        session: _Db,
        storage: _Storage,
        # Content-Length gate is resolved as a dep so it runs BEFORE
        # FastAPI parses the multipart body below — otherwise the
        # 2 MiB guard would run after the full upload was already in
        # memory, defeating the point.
        _content_length: _ContentLengthGuard,
        image: Annotated[UploadFile, File(description="Avatar image")],
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> AvatarResponse:
        """Store ``image`` as the caller's avatar, replacing any prior blob.

        Size-gate first (Content-Length + streaming), then content-type
        allowlist, then hash the bytes and hand them to the
        :class:`Storage` backend. The ``user`` row's
        ``avatar_blob_hash`` is updated to the new digest; the
        previous blob is NOT deleted — GC is a sweep concern.
        """
        if image.content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error": "avatar_content_type_rejected",
                    "message": (
                        f"content_type={image.content_type!r} is not one of "
                        f"{sorted(_ALLOWED_CONTENT_TYPES)}"
                    ),
                },
            )

        user = _resolve_session_user(
            session,
            request,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )

        payload = _read_capped(image)
        if not payload:
            # An empty multipart part is always a client bug. 400
            # (not 422) because the body is structurally valid
            # multipart — the problem is the *semantic* emptiness of
            # the image field.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "avatar_empty"},
            )

        digest = hashlib.sha256(payload).hexdigest()
        storage.put(
            digest,
            io.BytesIO(payload),
            content_type=image.content_type,
        )

        # The domain service layer would own this transition once an
        # avatar-service surfaces (cd-3i5 sibling); until then the
        # router writes directly. ``user`` is identity-scoped so no
        # tenant filter is at play.
        prior_hash = user.avatar_blob_hash
        user.avatar_blob_hash = digest
        session.flush()

        # Audit row lands inside the caller's UoW — the ``write_audit``
        # helper ``session.add``s without flushing, so a later raise
        # rolls it back alongside the ``user.avatar_blob_hash`` update.
        # Emitted unconditionally on POST (even when the new digest
        # matches the prior one — same bytes re-uploaded) because
        # replaying the endpoint IS a fresh operator intent, and the
        # row doubles as a per-upload access-log entry for forensic
        # review. Field names end in ``_hash`` so the log-redactor
        # (``app.util.redact``) treats them as already-minimised
        # digests and does not scrub the hex as a credential.
        write_audit(
            session,
            _identity_audit_ctx(),
            entity_kind="user",
            entity_id=user.id,
            action="identity.avatar.updated",
            diff={
                "user_id": user.id,
                "before_hash": prior_hash,
                "after_hash": digest,
                "content_type": image.content_type,
                "size_bytes": len(payload),
            },
        )

        return AvatarResponse(
            avatar_url=storage.sign_url(digest, ttl_seconds=_SIGN_TTL_SECONDS),
        )

    @router.delete(
        "/avatar",
        response_model=AvatarResponse,
        operation_id="auth.me.avatar.clear",
        summary="Clear the caller's avatar image (self-only)",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "avatar-clear",
                "summary": "Clear your avatar image",
                "mutates": True,
            },
            "x-agent-confirm": True,
        },
    )
    def delete_me_avatar(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> AvatarResponse:
        """Clear the caller's ``avatar_blob_hash``; the blob is left on disk.

        Idempotent — a caller clearing an already-null avatar still
        gets a ``200`` with ``avatar_url=None``. The preserved blob is
        eventually reaped by the GC sweep (see module docstring).
        """
        user = _resolve_session_user(
            session,
            request,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        prior_hash = user.avatar_blob_hash
        user.avatar_blob_hash = None
        session.flush()

        # Emit audit ONLY on an actual state transition. An idempotent
        # DELETE on an already-null avatar carries no information — the
        # log would just accumulate no-op rows on every retry from a
        # buggy client without telling an investigator anything new.
        # Mirrors the ``identity.session.invalidated`` shape in
        # :mod:`app.auth.session`: state-change gated, acting-user id
        # in the diff so the row is attributable.
        if prior_hash is not None:
            write_audit(
                session,
                _identity_audit_ctx(),
                entity_kind="user",
                entity_id=user.id,
                action="identity.avatar.cleared",
                diff={
                    "user_id": user.id,
                    "before_hash": prior_hash,
                },
            )
        return AvatarResponse(avatar_url=None)

    return router
