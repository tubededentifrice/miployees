"""Workspace-scoped user-management HTTP router (``/users/*``).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory.
Every route requires an active :class:`~app.tenancy.WorkspaceContext`.
Today's v1 surface:

* ``POST /users/invite`` — spec §03 "Additional users (invite →
  click-to-accept)". Inserts a pending ``invite`` row, mails the
  ``grant_invite`` magic link. A pending ``work_engagement`` row is
  NOT created at invite time — the accept-side path
  (:func:`app.domain.identity.membership._activate_invite`) seeds
  it once the invitee completes their passkey challenge.
* ``PATCH /users/{user_id}`` — partial profile update. Self-edits
  pass through without a capability check; edits targeting someone
  else require ``users.edit_profile_other`` (default
  ``owners, managers``).
* ``POST /users/{user_id}/archive`` — soft-archive the user's
  :class:`WorkEngagement` in this workspace PLUS every active
  :class:`UserWorkRole` they hold here. Idempotent.
* ``POST /users/{user_id}/reinstate`` — reverse archive. Idempotent.
  v1 implementation is workspace-local; the cross-workspace
  reinstate path that clears ``users.archived_at`` is tracked as a
  follow-up.
* ``DELETE /users/{user_id}/grants`` — removes every role_grant +
  permission_group_member + user_workspace row for ``user_id`` in
  the caller's workspace, plus revokes all sessions scoped there.
  Honours the last-owner guard.
* ``POST /users/{user_id}/magic_link`` — re-mails a ``recover_passkey``
  magic link to the user (cd-y5z3). Action gate
  ``users.edit_profile_other`` (default-allow: owners + managers,
  §05). Body ``{email_to_use?: str | null}`` overrides the destination.
* ``POST /users/{user_id}/reset_passkey`` — owner-initiated worker
  passkey reset (cd-y5z3, §03 "Owner-initiated worker passkey
  reset"). Action gate ``users.reset_passkey`` (default-allow:
  ``owners`` only). Sends two emails — the consumable enrolment
  link to the worker, a non-consumable notification copy to the
  calling owner.

Handler shape mirrors :mod:`app.api.v1.auth.signup` — unpack body,
call the domain service, map typed errors to HTTP symbols. The
UoW (:func:`app.api.deps.db_session`) owns the transaction
boundary; domain code never commits itself.

See ``docs/specs/12-rest-api.md`` §"Users",
``docs/specs/03-auth-and-tokens.md`` §"Additional users", and
``docs/specs/05-employees-and-roles.md`` §"Archive / reinstate".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.adapters.mail.ports import Mailer
from app.api.deps import current_workspace_context, db_session
from app.audit import write_audit
from app.auth import magic_link as magic_link_module
from app.auth._throttle import Throttle
from app.authz import PermissionDenied, require
from app.authz.dep import Permission
from app.config import Settings, get_settings
from app.domain.identity import membership
from app.domain.identity.permission_groups import (
    LastOwnerMember,
    write_member_remove_rejected_audit,
)
from app.mail.templates import passkey_reset_notice as passkey_reset_notice_template
from app.mail.templates import passkey_reset_worker as passkey_reset_worker_template
from app.mail.templates import recovery_new_link as recovery_new_link_template
from app.mail.templates import render as render_template
from app.services.employees import (
    EmployeeNotFound,
    EmployeeProfileUpdate,
    EmployeeView,
    ProfileFieldForbidden,
    archive_employee,
    get_employee,
    reinstate_employee,
    update_profile,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock

__all__ = [
    "EmployeeProfileResponse",
    "EmployeeUpdateRequest",
    "InviteRequest",
    "InviteResponse",
    "MagicLinkReissueRequest",
    "MagicLinkReissueResponse",
    "ResetPasskeyResponse",
    "build_users_router",
]


_log = logging.getLogger(__name__)


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class GrantInput(BaseModel):
    """One entry in the ``grants`` list on ``POST /users/invite``.

    v1 only accepts ``scope_kind='workspace'`` (property / organization
    scopes land in a follow-up). ``scope_id`` must match the caller's
    workspace — the domain service rejects mismatches with
    :class:`~app.domain.identity.membership.InviteBodyInvalid`.
    """

    scope_kind: str = Field("workspace", description="Always 'workspace' in v1.")
    scope_id: str = Field(..., description="Target workspace id (ULID).")
    grant_role: str = Field(
        ..., description="One of 'manager' | 'worker' | 'client' | 'guest'."
    )
    # Reserved fields present for spec parity; ignored by the v1
    # domain service. Keeping them on the wire means a follow-up
    # (organization-scope) lands without a breaking body shape.
    binding_org_id: str | None = None
    scope_property_id: str | None = None


class GroupMembershipInput(BaseModel):
    """One entry in the ``permission_group_memberships`` list."""

    group_id: str


class InviteRequest(BaseModel):
    """Request body for ``POST /users/invite``.

    ``work_engagement`` + ``user_work_roles`` are intentionally absent:
    their backing tables don't exist in Phase 1. A future body-shape
    bump will add them without breaking callers that send the current
    payload. Tracked as cd-1hd0.
    """

    email: str = Field(..., min_length=3, max_length=320)
    display_name: str = Field(..., min_length=1, max_length=160)
    grants: list[GrantInput]
    permission_group_memberships: list[GroupMembershipInput] | None = None


class InviteResponse(BaseModel):
    """Response body for ``POST /users/invite``."""

    invite_id: str
    pending_email: str
    user_id: str | None
    user_created: bool


class RemoveMemberResponse(BaseModel):
    """Response body for ``DELETE /users/{user_id}/grants``.

    Empty — a 204 would be more idiomatic but carrying a body lets
    the SPA assert on the symbolic outcome without introspecting the
    status code. Follow-up refactor may switch to 204.
    """

    status: str = "removed"


class EmployeeUpdateRequest(BaseModel):
    """Request body for ``PATCH /users/{user_id}``.

    Mirrors :class:`~app.services.employees.EmployeeProfileUpdate` —
    every field is optional and Pydantic's ``model_fields_set``
    distinguishes "omitted" from "explicitly set to None".
    ``extra='forbid'`` so unknown fields fail loud at 422 rather
    than being silently ignored. ``display_name=None`` is rejected
    at the DTO boundary via :meth:`_reject_display_name_null` so
    the NOT NULL contract on :class:`User` surfaces as a 422
    validation error rather than a 500.
    """

    model_config = {"extra": "forbid"}

    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _reject_display_name_null(self) -> EmployeeUpdateRequest:
        """Reject an explicit ``display_name=None`` — the column is NOT NULL."""
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be cleared; it is NOT NULL")
        return self


class EmployeeProfileResponse(BaseModel):
    """Response body for ``PATCH /users/{user_id}`` / ``GET /users/{user_id}``.

    Projection of :class:`~app.services.employees.EmployeeView` —
    carries the minimal identity-level fields the SPA needs plus the
    workspace-scoped engagement archival marker (derived from the
    active :class:`~app.adapters.db.workspace.models.WorkEngagement`
    row, if any).
    """

    id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    avatar_blob_hash: str | None
    engagement_archived_on: str | None
    created_at: str


class MagicLinkReissueRequest(BaseModel):
    """Request body for ``POST /users/{user_id}/magic_link``.

    Body is empty by default — re-issuing the magic link uses the
    target user's stored email. ``email_to_use`` is reserved so an
    operator can override the destination (e.g. send the link to a
    secondary address the user just confirmed) without first having
    to update :attr:`User.email`. Phase 1 honours the override only
    when explicitly set; an absent / null value falls back to the
    user's row.
    """

    model_config = {"extra": "forbid"}

    email_to_use: str | None = Field(
        default=None,
        description=(
            "Optional override destination address. When None / absent "
            "the link is mailed to the user's stored email."
        ),
    )


class MagicLinkReissueResponse(BaseModel):
    """Response body for ``POST /users/{user_id}/magic_link``.

    Carries the ``user_id`` (echoed for caller convenience) plus a
    ``status`` symbol the SPA can switch on. Status is always
    ``"sent"`` on the happy path; a future "queued" / "throttled"
    branch can land without breaking the contract.
    """

    user_id: str
    status: str = "sent"


class ResetPasskeyResponse(BaseModel):
    """Response body for ``POST /users/{user_id}/reset_passkey``.

    ``status`` is always ``"sent"`` on the happy path. The body is
    intentionally minimal — the operator-visible result lives in the
    audit row + the two emails delivered; the HTTP envelope just
    confirms the action started.
    """

    user_id: str
    status: str = "sent"


def _view_to_response(view: EmployeeView) -> EmployeeProfileResponse:
    """Project an :class:`EmployeeView` into the HTTP response shape."""
    return EmployeeProfileResponse(
        id=view.id,
        email=view.email,
        display_name=view.display_name,
        locale=view.locale,
        timezone=view.timezone,
        avatar_blob_hash=view.avatar_blob_hash,
        engagement_archived_on=(
            view.engagement_archived_on.isoformat()
            if view.engagement_archived_on is not None
            else None
        ),
        created_at=view.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_invite(exc: Exception) -> HTTPException:
    """Map a :func:`membership.invite` domain error to an HTTP response."""
    if isinstance(exc, membership.InviteBodyInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "invalid_body", "message": str(exc)},
        )
    # Rate-limited from the magic-link throttle is unlikely on this
    # route (per-invite throttle is cheap) but we map it for safety.
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _http_for_remove(exc: Exception) -> HTTPException:
    """Map a :func:`membership.remove_member` error to an HTTP response.

    :class:`LastOwnerMember` is **not** handled here: it is a
    :class:`~app.domain.errors.Validation` subclass, so the RFC 7807
    exception handler in :mod:`app.api.errors` translates it directly
    into a 422 ``would_orphan_owners_group`` problem+json envelope.
    The router still intercepts it to write the forensic rejection
    audit row on a fresh UoW, then re-raises the typed exception.
    """
    if isinstance(exc, membership.NotAMember):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_a_member"},
        )
    if isinstance(exc, membership.InviteStateInvalid):
        # ``InviteStateInvalid`` is re-raised when the workspace has
        # no owners group — a server-side invariant break.
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "workspace_invariant_broken"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request``."""
    if request.client is None:
        return ""
    return request.client.host


def _resolve_inviter_display_name(session: Session, *, user_id: str) -> str:
    """Return the inviter's display name for the invite email copy."""
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        return "A crew.day user"
    return user.display_name


def _resolve_workspace_name(session: Session, *, workspace_id: str) -> str:
    """Return the workspace display name for the invite email copy."""
    row = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    if row is None:
        return "your workspace"
    return row.name


def _assert_workspace_membership(
    session: Session, *, ctx: WorkspaceContext, user_id: str
) -> UserWorkspace:
    """Return the (user, workspace) :class:`UserWorkspace` row or raise 404.

    Mirrors :func:`app.services.employees._assert_membership` — the
    ``magic_link`` and ``reset_passkey`` routes both target a user
    inside the caller's workspace, and a cross-workspace probe must
    collapse to 404 ``employee_not_found`` rather than leaking the
    user's existence (or absence) on a sibling tenant.
    """
    row = session.get(UserWorkspace, (user_id, ctx.workspace_id))
    if row is None or row.workspace_id != ctx.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "employee_not_found"},
        )
    return row


def _load_user(session: Session, *, user_id: str) -> User:
    """Return the :class:`User` row for ``user_id`` or raise 404.

    ``user`` is identity-scoped (not workspace-scoped); membership
    against the caller's workspace MUST be verified independently
    before this is called.
    """
    with tenant_agnostic():
        row = session.get(User, user_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "employee_not_found"},
        )
    return row


def _mask_email_for_notice(email: str) -> str:
    """Return ``email`` with the local part collapsed for the notice copy.

    Spec §03 "Owner-initiated worker passkey reset" pins the masked
    shape (e.g. ``m***@example.com``). Keeps the first character of
    the local part so the owner can do a quick sanity-check; replaces
    the rest with three asterisks. Defensive against missing ``@`` —
    in that case we collapse the whole string to ``"***"``.
    """
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


class _CapturingMailer:
    """Recording :class:`Mailer` that intercepts the magic-link send.

    Mirrors the pattern in :class:`app.auth.recovery._CapturingMailer`.
    :func:`magic_link.request_link` mints the token and inserts the
    nonce row; we re-use that mint pipeline (one source of truth for
    token layout + rate-limiting + audit) but capture the rendered
    body to recover the URL — which our caller then re-frames using
    its own template.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self.captured_url: str | None = None

    def send(
        self,
        *,
        to: object,
        subject: object,
        body_text: str,
        body_html: object = None,
        headers: object = None,
        reply_to: object = None,
    ) -> str:
        del to, subject, body_html, headers, reply_to
        prefix = self._base_url.rstrip("/")
        for line in body_text.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                self.captured_url = stripped
                return "captured"
        # Defensive — magic-link's template always carries a URL.
        # Failing loudly here is better than silently shipping a
        # tokenless mail.
        raise RuntimeError("users router capture: magic-link body did not carry a URL")


def _issue_passkey_recovery_link(
    session: Session,
    *,
    user: User,
    target_email: str,
    ip: str,
    base_url: str,
    throttle: Throttle,
    settings: Settings,
) -> str:
    """Mint a ``recover_passkey`` magic link for ``user`` and return the URL.

    Reuses :func:`magic_link.request_link` for the token mint + nonce
    insert + ``audit.magic_link.sent`` audit row (single source of
    truth for token layout, rate-limit, and forensic data). The
    caller renders + sends the email itself so the body copy can
    differentiate "manager re-issued a magic link" from
    "self-service recovery".

    ``target_email`` is the address the magic-link service will
    pepper-hash for its own audit row; ``user.email`` is the canonical
    form pinned to the row, so we always pass that — the caller does
    not get to override the hash anchor (the override on the HTTP
    surface only affects the destination of the rendered email, not
    the forensic identity of the link).

    The magic-link send is intercepted by :class:`_CapturingMailer`
    (an in-process intercept that stores the rendered URL without
    touching SMTP), so firing it synchronously here is safe: no
    real mail leaves the host. The outer manager-reissue template
    is the actual SMTP touch and lives in the calling router, where
    it is queued post-commit via :class:`PendingDispatch` (cd-9slq).
    """
    del target_email  # reserved for a future override-aware nonce hash
    capture = _CapturingMailer(base_url=base_url)
    # The capturing mailer is a synchronous in-process intercept (no
    # SMTP, no network) — its :meth:`send` only stores the rendered
    # URL line. Firing :meth:`PendingMagicLink.deliver` here merely
    # threads the URL through the closure and back to us via
    # ``capture.captured_url``; no email leaves the host. The outer
    # recovery-template send (which *does* hit SMTP) is queued
    # post-commit on the calling router's :class:`PendingDispatch`.
    pending = magic_link_module.request_link(
        session,
        email=user.email,
        purpose="recover_passkey",
        ip=ip,
        mailer=capture,
        base_url=base_url,
        throttle=throttle,
        settings=settings,
        subject_id=user.id,
    )
    if pending is not None:
        pending.deliver()
    if capture.captured_url is None:  # pragma: no cover - defensive
        raise RuntimeError("users router: magic-link service produced no URL")
    return capture.captured_url


def _send_recovery_link_email(
    *,
    mailer: Mailer,
    to_email: str,
    display_name: str,
    url: str,
) -> None:
    """Render + send the standard recovery template (cd-y5z3 magic_link reissue).

    The owner-initiated reissue uses the same body the self-service
    recovery flow uses — the worker's mailbox already understands
    "open this link to enrol a fresh passkey", so a separate template
    would just diverge wording without adding signal. The 15-minute
    TTL string matches the magic-link service's per-purpose ceiling
    for ``recover_passkey``.
    """
    subject = render_template(recovery_new_link_template.SUBJECT)
    body_text = render_template(
        recovery_new_link_template.BODY_TEXT,
        display_name=display_name,
        url=url,
        ttl_minutes="10",
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


def _send_passkey_reset_worker_email(
    *,
    mailer: Mailer,
    to_email: str,
    worker_display_name: str,
    owner_display_name: str,
    workspace_name: str,
    url: str,
) -> None:
    """Render + send the worker-side passkey-reset email."""
    subject = render_template(passkey_reset_worker_template.SUBJECT)
    body_text = render_template(
        passkey_reset_worker_template.BODY_TEXT,
        display_name=worker_display_name,
        owner_display_name=owner_display_name,
        workspace_name=workspace_name,
        url=url,
        ttl_minutes="10",
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


def _send_passkey_reset_notice_email(
    *,
    mailer: Mailer,
    to_email: str,
    owner_display_name: str,
    worker_display_name: str,
    worker_email_masked: str,
    workspace_name: str,
    timestamp: str,
    notice_url: str,
) -> None:
    """Render + send the owner-side notification copy.

    Carries no consumable token — the link inside lands on
    ``/recover/notice`` per spec §03 "Owner-initiated worker passkey
    reset". The body explicitly tells the owner clicking the URL is
    NOT an enrolment action.
    """
    subject = render_template(passkey_reset_notice_template.SUBJECT)
    body_text = render_template(
        passkey_reset_notice_template.BODY_TEXT,
        owner_display_name=owner_display_name,
        worker_display_name=worker_display_name,
        worker_email_masked=worker_email_masked,
        workspace_name=workspace_name,
        timestamp=timestamp,
        notice_url=notice_url,
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_users_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``mailer`` + ``throttle``.

    Mounted by the v1 app factory at
    ``/w/<slug>/api/v1/users``. Tests instantiate it directly with
    a recording mailer + per-case throttle for isolation.
    """
    # Tags: ``identity`` (spec §01 context map) surfaces every
    # identity-adjacent operation under one OpenAPI section;
    # ``users`` is kept for back-compat with existing clients that
    # filter on the finer-grained tag.
    router = APIRouter(prefix="/users", tags=["identity", "users"])
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    @router.post(
        "/invite",
        status_code=status.HTTP_201_CREATED,
        response_model=InviteResponse,
        summary="Invite a user to the caller's workspace",
    )
    def post_invite(
        body: InviteRequest,
        request: Request,
        ctx: _Ctx,
    ) -> InviteResponse:
        """Create or refresh a pending invite and mail the magic link.

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``
        so the invite-flavoured SMTP send fires only after the
        invite row + magic-link nonce + ``user.invited`` audit are
        durable. A commit failure short-circuits
        ``dispatch.deliver()`` so no working invite token reaches
        the inbox without the matching invite row.
        """
        del request  # ip currently unused; magic_link re-derives internally
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )

        grants_payload: list[dict[str, Any]] = [g.model_dump() for g in body.grants]
        memberships_payload: list[dict[str, Any]] = [
            gm.model_dump() for gm in (body.permission_group_memberships or [])
        ]

        dispatch = magic_link_module.PendingDispatch()
        try:
            with make_uow() as session:
                assert isinstance(session, Session)
                inviter_display_name = _resolve_inviter_display_name(
                    session, user_id=ctx.actor_id
                )
                workspace_name = _resolve_workspace_name(
                    session, workspace_id=ctx.workspace_id
                )

                outcome = membership.invite(
                    session,
                    ctx,
                    email=body.email,
                    display_name=body.display_name,
                    grants=grants_payload,
                    group_memberships=memberships_payload,
                    mailer=mailer,
                    throttle=throttle,
                    base_url=resolved_base_url,
                    settings=cfg,
                    inviter_display_name=inviter_display_name,
                    workspace_name=workspace_name,
                    dispatch=dispatch,
                )
        except membership.InviteBodyInvalid as exc:
            raise _http_for_invite(exc) from exc

        # ``with`` exited cleanly → UoW committed → invite + nonce +
        # audit are durable on disk. Now fire the queued invite-
        # flavoured template post-commit (cd-9slq).
        dispatch.deliver()

        return InviteResponse(
            invite_id=outcome.id,
            pending_email=outcome.pending_email,
            user_id=outcome.user_id,
            user_created=outcome.user_created,
        )

    @router.patch(
        "/{user_id}",
        response_model=EmployeeProfileResponse,
        summary="Update the profile of a user in the caller's workspace",
    )
    def patch_user(
        user_id: str,
        body: EmployeeUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Partial profile update.

        Routes-level contract:

        * 200 + refreshed view on success.
        * 404 ``employee_not_found`` when the target user is not a
          member of the caller's workspace.
        * 403 ``forbidden`` when the caller is neither the target nor
          holds ``users.edit_profile_other``.
        * 422 on DTO validation failure (pydantic).
        """
        # Re-emit the request body into the service DTO. The HTTP
        # shape and the service shape are intentionally identical so
        # this is a straight passthrough; keeping the two types
        # distinct lets us evolve either surface without touching
        # the other.
        sent_fields = body.model_fields_set
        service_body = EmployeeProfileUpdate.model_validate(
            {f: getattr(body, f) for f in sent_fields}
        )

        try:
            view = update_profile(
                session,
                ctx,
                user_id=user_id,
                body=service_body,
            )
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except ProfileFieldForbidden as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.post(
        "/{user_id}/archive",
        response_model=EmployeeProfileResponse,
        summary="Archive a user's engagement + work roles in this workspace",
    )
    def post_archive(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Soft-archive + idempotent. See §05 "Archive / reinstate"."""
        try:
            view = archive_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.post(
        "/{user_id}/reinstate",
        response_model=EmployeeProfileResponse,
        summary="Reinstate a user's engagement + work roles in this workspace",
    )
    def post_reinstate(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Reverse archive. Idempotent. See §05 "Archive / reinstate"."""
        try:
            view = reinstate_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.get(
        "/{user_id}",
        response_model=EmployeeProfileResponse,
        summary="Read a user's workspace-scoped profile",
    )
    def get_user(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Read-only projection the SPA keys off for the profile page."""
        try:
            view = get_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        return _view_to_response(view)

    @router.delete(
        "/{user_id}/grants",
        response_model=RemoveMemberResponse,
        summary="Remove a user's grants + sessions in the caller's workspace",
    )
    def delete_grants(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> RemoveMemberResponse:
        """Strip every grant + group-member + session tied to ``user_id``."""
        try:
            membership.remove_member(session, ctx, user_id=user_id)
        except LastOwnerMember:
            # Typed refusal — forensic row lands on a fresh UoW so
            # the primary UoW's rollback does not sweep it. Re-raise
            # the typed exception so the RFC 7807 seam translates it
            # into a 422 ``would_orphan_owners_group`` envelope.
            try:
                with make_uow() as fresh:
                    assert isinstance(fresh, Session)
                    write_member_remove_rejected_audit(
                        fresh,
                        ctx,
                        group_id=_resolve_owners_group_id(session, ctx.workspace_id)
                        or "",
                        user_id=user_id,
                        reason="would_orphan_owners_group",
                    )
            except Exception:
                _log.exception("remove_member refusal audit failed on fresh UoW")
            raise
        except (membership.NotAMember, membership.InviteStateInvalid) as exc:
            raise _http_for_remove(exc) from exc

        return RemoveMemberResponse()

    @router.post(
        "/{user_id}/magic_link",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=MagicLinkReissueResponse,
        operation_id="users.magic_link.issue",
        summary="Re-mail a recovery magic link to a user (manager+)",
        # ``users.edit_profile_other`` (default-allow: owners + managers)
        # gates the action — re-issuing a magic link is the same authority
        # tier as editing someone else's profile (§12 "Users").
        dependencies=[
            Depends(Permission("users.edit_profile_other", scope_kind="workspace"))
        ],
    )
    def post_magic_link(
        user_id: str,
        body: MagicLinkReissueRequest | None,
        request: Request,
        ctx: _Ctx,
    ) -> MagicLinkReissueResponse:
        """Re-mail a ``recover_passkey`` magic link to ``user_id``.

        Authority gate: ``users.edit_profile_other`` (default-allow
        owners + managers, per §05 catalog). Workers who hit this
        route fall through to 403 ``permission_denied``.

        Membership: the target must be a member of the caller's
        workspace; a cross-tenant call collapses to 404
        ``employee_not_found`` (matches the rest of the user-targeting
        surface).

        Audit: writes ``user.magic_link.issued`` with the actor + the
        subject user id. The magic-link service writes its own
        ``magic_link.sent`` row under an agnostic ctx — both rows
        land in the same UoW.

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``.
        The reissue-template SMTP send fires only after the magic-
        link nonce + audit rows commit; a commit failure short-
        circuits ``dispatch.deliver()`` and no working magic-link
        token reaches the user inbox without a matching nonce.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )

        dispatch = magic_link_module.PendingDispatch()
        with make_uow() as session:
            assert isinstance(session, Session)
            _assert_workspace_membership(session, ctx=ctx, user_id=user_id)
            user = _load_user(session, user_id=user_id)

            # ``email_to_use`` overrides the destination only — never the
            # forensic anchor (the magic-link nonce always hashes the
            # canonical user.email so abuse correlation joins cleanly).
            target_email = (
                body.email_to_use
                if (body is not None and body.email_to_use)
                else user.email
            )

            url = _issue_passkey_recovery_link(
                session,
                user=user,
                target_email=target_email,
                ip=_client_ip(request),
                base_url=resolved_base_url,
                throttle=throttle,
                settings=cfg,
            )

            # Capture every input :func:`_send_recovery_link_email`
            # needs at mint time so the deferred send is a parameter-
            # free closure the dispatch can fire post-commit.
            captured_mailer = mailer
            captured_to_email = target_email
            captured_display_name = user.display_name
            captured_url = url

            def _deferred_reissue_send() -> None:
                _send_recovery_link_email(
                    mailer=captured_mailer,
                    to_email=captured_to_email,
                    display_name=captured_display_name,
                    url=captured_url,
                )

            dispatch.add_callback(_deferred_reissue_send)

            write_audit(
                session,
                ctx,
                entity_kind="user",
                entity_id=user_id,
                action="user.magic_link.issued",
                diff={
                    "subject_user_id": user_id,
                    "actor_user_id": ctx.actor_id,
                    "purpose": "recover_passkey",
                    # ``email_to_use`` is logged as a boolean discriminator
                    # — the plaintext lives in the magic-link nonce + this
                    # endpoint's mailer call, never in the audit diff
                    # (§15 PII minimisation).
                    "destination_overridden": bool(
                        body is not None and body.email_to_use
                    ),
                },
            )
        # ``with`` exited cleanly → UoW committed → magic-link nonce +
        # ``user.magic_link.issued`` audit are durable on disk. Only
        # now do we fire the reissue-template SMTP send (cd-9slq);
        # :meth:`PendingDispatch.deliver` swallows MailDeliveryError
        # so a relay outage doesn't shadow the 202.
        dispatch.deliver()

        return MagicLinkReissueResponse(user_id=user_id)

    @router.post(
        "/{user_id}/reset_passkey",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ResetPasskeyResponse,
        operation_id="users.reset_passkey",
        summary="Owner-initiated worker passkey reset (owners only)",
    )
    def post_reset_passkey(
        user_id: str,
        request: Request,
        ctx: _Ctx,
    ) -> ResetPasskeyResponse:
        """Trigger an owner-initiated passkey reset for ``user_id``.

        Spec §03 "Owner-initiated worker passkey reset". Authority
        gate: ``users.reset_passkey`` (default-allow ``owners`` only,
        per §05 catalog). Managers fall through to 403; the lighter
        re-issue surface (``POST /{user_id}/magic_link``) remains
        available to them.

        Side effects (one UoW):

        1. Mint a ``recover_passkey`` magic link bound to the worker.
        2. Send the worker the real magic link (claimable).
        3. Send the owner a non-consumable notification copy with
           the worker's email masked.
        4. Audit ``user.reset_passkey.initiated`` with subject + actor.

        Membership / 404 / 403 vocabulary matches the sibling
        ``/{user_id}/magic_link`` route.

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``
        so both SMTP sends fire only after the magic-link nonce +
        ``user.reset_passkey.initiated`` audit rows commit. A commit
        failure short-circuits ``dispatch.deliver()`` so neither the
        worker's claimable link nor the owner's notice leaves the
        host with rolled-back state.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )

        dispatch = magic_link_module.PendingDispatch()
        with make_uow() as session:
            assert isinstance(session, Session)
            # Owner-only gate, performed inline (not via the
            # :class:`Permission` dependency) because the action key was
            # added in this slice and we want the test to drive the
            # default_allow=("owners",) path explicitly. Equivalent to
            # ``Depends(Permission("users.reset_passkey", scope_kind="workspace"))``;
            # kept inline so a future shift to the FastAPI dep is a single
            # import swap without restructuring the body.
            try:
                require(
                    session,
                    ctx,
                    action_key="users.reset_passkey",
                    scope_kind="workspace",
                    scope_id=ctx.workspace_id,
                )
            except PermissionDenied as exc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "permission_denied",
                        "action_key": "users.reset_passkey",
                    },
                ) from exc

            _assert_workspace_membership(session, ctx=ctx, user_id=user_id)
            worker = _load_user(session, user_id=user_id)
            owner = _load_user(session, user_id=ctx.actor_id)
            workspace_name = _resolve_workspace_name(
                session, workspace_id=ctx.workspace_id
            )

            # Mint the (single) consumable magic link for the worker. The
            # owner's notification copy carries no token — it points at
            # ``/recover/notice`` so a forwarded copy lands on a "this is
            # the notice, not the link" page rather than a redeemable
            # ceremony (§03).
            worker_url = _issue_passkey_recovery_link(
                session,
                user=worker,
                target_email=worker.email,
                ip=_client_ip(request),
                base_url=resolved_base_url,
                throttle=throttle,
                settings=cfg,
            )
            notice_url = f"{resolved_base_url.rstrip('/')}/recover/notice"
            now = SystemClock().now()

            # Capture every input the deferred sends need at mint time
            # so the dispatch entries are parameter-free closures.
            # :meth:`PendingDispatch.deliver` swallows per-entry
            # MailDeliveryError so a relay failure on the worker side
            # does NOT abort the owner's notification (and vice versa)
            # — the same independent-send guarantee the prior
            # try/except blocks gave us, now lifted post-commit.
            captured_mailer = mailer
            captured_worker_email = worker.email
            captured_worker_display_name = worker.display_name
            captured_owner_email = owner.email
            captured_owner_display_name = owner.display_name
            captured_worker_email_masked = _mask_email_for_notice(worker.email)
            captured_workspace_name = workspace_name
            captured_now_iso = now.isoformat()
            captured_worker_url = worker_url
            captured_notice_url = notice_url

            def _deferred_worker_send() -> None:
                _send_passkey_reset_worker_email(
                    mailer=captured_mailer,
                    to_email=captured_worker_email,
                    worker_display_name=captured_worker_display_name,
                    owner_display_name=captured_owner_display_name,
                    workspace_name=captured_workspace_name,
                    url=captured_worker_url,
                )

            def _deferred_owner_send() -> None:
                _send_passkey_reset_notice_email(
                    mailer=captured_mailer,
                    to_email=captured_owner_email,
                    owner_display_name=captured_owner_display_name,
                    worker_display_name=captured_worker_display_name,
                    worker_email_masked=captured_worker_email_masked,
                    workspace_name=captured_workspace_name,
                    timestamp=captured_now_iso,
                    notice_url=captured_notice_url,
                )

            dispatch.add_callback(_deferred_worker_send)
            dispatch.add_callback(_deferred_owner_send)

            write_audit(
                session,
                ctx,
                entity_kind="user",
                entity_id=user_id,
                action="user.reset_passkey.initiated",
                diff={
                    "subject_user_id": user_id,
                    "actor_user_id": ctx.actor_id,
                    "workspace_id": ctx.workspace_id,
                    # No plaintext emails, no plaintext IPs — the magic-
                    # link nonce already carries the forensic hashes
                    # under its own audit row, and re-recording them here
                    # would double the PII surface for no gain.
                },
            )
        # ``with`` exited cleanly → UoW committed → magic-link nonce +
        # ``user.reset_passkey.initiated`` audit are durable. Now fire
        # the two queued SMTP sends post-commit (cd-9slq).
        dispatch.deliver()

        return ResetPasskeyResponse(user_id=user_id)

    return router


def _resolve_owners_group_id(session: Session, workspace_id: str) -> str | None:
    """Return the ``owners`` group id for the workspace or ``None``."""
    from app.adapters.db.authz.models import PermissionGroup

    row = session.scalar(
        select(PermissionGroup).where(
            PermissionGroup.workspace_id == workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    )
    return row.id if row is not None else None
