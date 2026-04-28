"""Identity-scoped privacy export endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.storage.ports import Storage
from app.api.deps import db_session, get_storage
from app.api.v1.auth.me_tokens import _resolve_session_user
from app.domain.privacy import get_user_export, request_user_export

__all__ = ["PrivacyExportResponse", "build_me_export_router"]


_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]


class PrivacyExportResponse(BaseModel):
    id: str
    status: str
    poll_url: str
    download_url: str | None = None
    expires_at: datetime | None = None


def build_me_export_router() -> APIRouter:
    router = APIRouter(prefix="/me", tags=["identity", "me", "privacy"])

    @router.post(
        "/export",
        response_model=PrivacyExportResponse,
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="me.export.request",
        summary="Request a privacy export for the current user",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "export",
                "summary": "Request my privacy export",
            }
        },
    )
    def post_export(
        request: Request,
        session: _Db,
        storage: _Storage,
        crewday_session: Annotated[str | None, Cookie(alias="crewday_session")] = None,
        host_session: Annotated[
            str | None, Cookie(alias="__Host-crewday_session")
        ] = None,
    ) -> PrivacyExportResponse:
        user_id = _resolve_session_user(
            session,
            cookie_primary=host_session,
            cookie_dev=crewday_session,
        )
        result = request_user_export(
            session,
            storage,
            user_id=user_id,
            poll_base_path=str(request.url_for("get_export", export_id="")).rstrip("/"),
        )
        return PrivacyExportResponse(**result.__dict__)

    @router.get(
        "/export/{export_id}",
        response_model=PrivacyExportResponse,
        operation_id="me.export.get",
        summary="Poll a privacy export",
        openapi_extra={
            "x-cli": {
                "group": "me",
                "verb": "export-status",
                "summary": "Poll my privacy export",
            }
        },
    )
    def get_export(
        export_id: str,
        session: _Db,
        storage: _Storage,
        crewday_session: Annotated[str | None, Cookie(alias="crewday_session")] = None,
        host_session: Annotated[
            str | None, Cookie(alias="__Host-crewday_session")
        ] = None,
    ) -> PrivacyExportResponse:
        user_id = _resolve_session_user(
            session,
            cookie_primary=host_session,
            cookie_dev=crewday_session,
        )
        result = get_user_export(
            session,
            storage,
            user_id=user_id,
            export_id=export_id,
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "export_not_found"},
            )
        return PrivacyExportResponse(**result.__dict__)

    return router
