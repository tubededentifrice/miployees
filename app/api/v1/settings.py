"""Workspace settings surface for the manager Settings page.

Spec §12 pins these routes at the top of the workspace API tree:

```
GET   /settings
PATCH /settings
GET   /settings/catalog
```

The four workspace identity fields (`name`, `default_timezone`,
`default_locale`, `default_currency`) already live on first-class
columns and are edited through `app.services.workspace.settings_service`.
This router exposes the broader structured settings cascade held in
`workspace.settings_json`, merged with the catalog defaults so the SPA
can render one concrete workspace-default map.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, RootModel
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.audit import write_audit
from app.authz.dep import Permission
from app.events.bus import bus as default_event_bus
from app.events.types import WorkspaceChanged
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock

__all__ = [
    "SettingDefinitionResponse",
    "WorkspaceSettingsResponse",
    "build_settings_router",
]

router = APIRouter(tags=["settings"])

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

SettingType = Literal["enum", "int", "bool"]


class SettingDefinitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: SettingType
    catalog_default: Any
    enum_values: list[str] | None
    override_scope: str
    description: str
    spec: str


class WorkspaceSettingsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    timezone: str
    currency: str
    country: str
    default_locale: str


class WorkspaceSettingsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approvals: dict[str, list[str]]
    danger_zone: list[str]


class WorkspaceSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: WorkspaceSettingsMeta
    defaults: dict[str, Any]
    policy: WorkspaceSettingsPolicy


class WorkspaceSettingsPatch(RootModel[dict[str, Any]]):
    """Partial workspace-default update.

    The root object is a map of catalog key to value. `null` restores
    the catalog default by removing the workspace override.
    """


_CATALOG: tuple[SettingDefinitionResponse, ...] = (
    SettingDefinitionResponse(
        key="evidence.policy",
        label="Evidence policy",
        type="enum",
        catalog_default="optional",
        enum_values=["require", "optional", "forbid"],
        override_scope="W/P/U/WE/T",
        description="Whether tasks require photo or file evidence.",
        spec="§05, §06",
    ),
    SettingDefinitionResponse(
        key="bookings.pay_basis",
        label="Booking pay basis",
        type="enum",
        catalog_default="scheduled",
        enum_values=["scheduled", "actual"],
        override_scope="W/WE",
        description="How booking pay is computed by default.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="bookings.auto_approve_overrun_minutes",
        label="Auto-approve overrun",
        type="int",
        catalog_default=30,
        enum_values=None,
        override_scope="W/WE",
        description="Minutes of overrun that can be approved automatically.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="bookings.cancellation_window_hours",
        label="Cancellation window",
        type="int",
        catalog_default=24,
        enum_values=None,
        override_scope="W",
        description="Hours before a booking when cancellation rules apply.",
        spec="§09, §22",
    ),
    SettingDefinitionResponse(
        key="bookings.cancellation_fee_pct",
        label="Cancellation fee",
        type="int",
        catalog_default=50,
        enum_values=None,
        override_scope="W",
        description="Default cancellation fee percentage.",
        spec="§09, §22",
    ),
    SettingDefinitionResponse(
        key="bookings.cancellation_pay_to_worker",
        label="Pay worker on cancellation",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/WE",
        description="Whether cancellation fees flow to the worker.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="pay.frequency",
        label="Pay frequency",
        type="enum",
        catalog_default="monthly",
        enum_values=["weekly", "fortnightly", "monthly"],
        override_scope="W",
        description="Default payroll period cadence.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="pay.allow_self_manage_destinations",
        label="Self-manage payout destinations",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/WE",
        description="Whether workers can manage their own payout destinations.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="pay.week_start",
        label="Week start",
        type="enum",
        catalog_default="monday",
        enum_values=["monday", "sunday"],
        override_scope="W",
        description="Start day for payroll and reporting weeks.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="retention.audit_days",
        label="Audit retention",
        type="int",
        catalog_default=730,
        enum_values=None,
        override_scope="W",
        description="Days to retain audit rows before archival.",
        spec="§02",
    ),
    SettingDefinitionResponse(
        key="retention.llm_calls_days",
        label="LLM call retention",
        type="int",
        catalog_default=90,
        enum_values=None,
        override_scope="W",
        description="Days to retain LLM call records.",
        spec="§02, §11",
    ),
    SettingDefinitionResponse(
        key="retention.task_photos_days",
        label="Task photo retention",
        type="int",
        catalog_default=365,
        enum_values=None,
        override_scope="W",
        description="Days to retain task photo evidence.",
        spec="§02",
    ),
    SettingDefinitionResponse(
        key="retention.template_revisions_days",
        label="Template revision retention",
        type="int",
        catalog_default=365,
        enum_values=None,
        override_scope="W",
        description="Days to retain hash-self-seeded revision history.",
        spec="§02",
    ),
    SettingDefinitionResponse(
        key="scheduling.horizon_days",
        label="Scheduling horizon",
        type="int",
        catalog_default=30,
        enum_values=None,
        override_scope="W/P",
        description="Days ahead to materialise scheduled work.",
        spec="§06",
    ),
    SettingDefinitionResponse(
        key="tasks.checklist_required",
        label="Checklist required",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/P/U/WE/T",
        description="Whether task checklists must be completed.",
        spec="§05",
    ),
    SettingDefinitionResponse(
        key="tasks.allow_complete_backdated",
        label="Allow backdated completion",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/P/U/WE",
        description="Whether workers can complete work in the past.",
        spec="§05",
    ),
    SettingDefinitionResponse(
        key="tasks.allow_skip_with_reason",
        label="Allow skip with reason",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/P/U/WE",
        description="Whether workers can skip a task with a reason.",
        spec="§05",
    ),
    SettingDefinitionResponse(
        key="tasks.overdue_grace_minutes",
        label="Overdue grace",
        type="int",
        catalog_default=15,
        enum_values=None,
        override_scope="W",
        description="Minutes after a task ends before it flips overdue.",
        spec="§06",
    ),
    SettingDefinitionResponse(
        key="tasks.overdue_tick_seconds",
        label="Overdue tick cadence",
        type="int",
        catalog_default=300,
        enum_values=None,
        override_scope="W",
        description="Worker cadence for scanning overdue tasks.",
        spec="§16",
    ),
    SettingDefinitionResponse(
        key="inventory.apply_on_task",
        label="Apply inventory on task",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/P/U/WE/T",
        description="Whether task completion applies inventory effects.",
        spec="§08",
    ),
    SettingDefinitionResponse(
        key="inventory.shrinkage_alert_pct",
        label="Shrinkage alert",
        type="int",
        catalog_default=10,
        enum_values=None,
        override_scope="W/P",
        description="Thirty-day shrinkage percentage that triggers a digest alert.",
        spec="§08",
    ),
    SettingDefinitionResponse(
        key="expenses.autofill_receipts",
        label="Autofill receipts",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/WE",
        description="Whether receipt OCR/autofill is enabled.",
        spec="§09",
    ),
    SettingDefinitionResponse(
        key="chat.enabled",
        label="Chat enabled",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/WE",
        description="Whether chat is enabled for the workspace.",
        spec="§11",
    ),
    SettingDefinitionResponse(
        key="voice.enabled",
        label="Voice enabled",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/WE",
        description="Whether voice transcription is enabled.",
        spec="§11",
    ),
    SettingDefinitionResponse(
        key="notifications.email_digest",
        label="Email digest",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W/WE",
        description="Whether daily email digests are enabled.",
        spec="§10",
    ),
    SettingDefinitionResponse(
        key="assets.warranty_alert_days",
        label="Warranty alert days",
        type="int",
        catalog_default=30,
        enum_values=None,
        override_scope="W/P",
        description="Days before an asset warranty expiry to alert.",
        spec="§21",
    ),
    SettingDefinitionResponse(
        key="assets.show_guest_assets",
        label="Show guest assets",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/P/U",
        description="Whether guest welcome pages can include assets.",
        spec="§21",
    ),
    SettingDefinitionResponse(
        key="auth.self_service_recovery_enabled",
        label="Self-service recovery",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W",
        description="Whether users can recover access without manager help.",
        spec="§03",
    ),
    SettingDefinitionResponse(
        key="auth.passkey_rollback_auto_revoke",
        label="Passkey rollback auto-revoke",
        type="bool",
        catalog_default=True,
        enum_values=None,
        override_scope="W",
        description="Whether rollback recovery auto-revokes stale passkeys.",
        spec="§15",
    ),
    SettingDefinitionResponse(
        key="ical.allow_self_signed",
        label="Allow self-signed iCal",
        type="bool",
        catalog_default=False,
        enum_values=None,
        override_scope="W/P",
        description="Whether iCal polling accepts self-signed TLS certificates.",
        spec="§04",
    ),
    SettingDefinitionResponse(
        key="webhook.outbound.signing_window_minutes",
        label="Webhook signing window",
        type="int",
        catalog_default=5,
        enum_values=None,
        override_scope="W",
        description="Minutes an outbound webhook signature remains valid.",
        spec="§10",
    ),
    SettingDefinitionResponse(
        key="webhook.outbound.secret_rotation_window_hours",
        label="Webhook secret rotation window",
        type="int",
        catalog_default=24,
        enum_values=None,
        override_scope="W",
        description="Hours old and new outbound webhook secrets overlap.",
        spec="§10",
    ),
)

_CATALOG_BY_KEY = {item.key: item for item in _CATALOG}
_ALWAYS_GATED_ACTIONS = [
    "payout_destination.change_default",
    "vendor_invoice.pay",
    "workspace.archive",
    "permission_group.membership.change",
]
_CONFIGURABLE_APPROVAL_ACTIONS = [
    "expenses.create",
    "tasks.complete",
    "inventory.adjust",
    "booking.amend",
]
_DANGER_ZONE = [
    "Backup restore",
    "Root key rotation",
    "Workspace archive",
    "Hard-delete purge",
]


def build_settings_router() -> APIRouter:
    return router


@router.get(
    "/settings/catalog",
    response_model=list[SettingDefinitionResponse],
    operation_id="settings.catalog",
    summary="List workspace settings catalog",
    dependencies=[Depends(Permission("scope.edit_settings", scope_kind="workspace"))],
)
def get_settings_catalog() -> list[SettingDefinitionResponse]:
    return list(_CATALOG)


@router.get(
    "/settings",
    response_model=WorkspaceSettingsResponse,
    operation_id="settings.workspace.read",
    summary="Read workspace settings",
    dependencies=[Depends(Permission("scope.edit_settings", scope_kind="workspace"))],
)
def get_workspace_settings(ctx: _Ctx, session: _Db) -> WorkspaceSettingsResponse:
    return _workspace_settings_response(_get_workspace(session, ctx))


@router.patch(
    "/settings",
    response_model=WorkspaceSettingsResponse,
    operation_id="settings.workspace.patch",
    summary="Patch workspace settings",
    dependencies=[Depends(Permission("scope.edit_settings", scope_kind="workspace"))],
)
def patch_workspace_settings(
    payload: WorkspaceSettingsPatch,
    ctx: _Ctx,
    session: _Db,
) -> WorkspaceSettingsResponse:
    ws = _get_workspace(session, ctx)
    current = _coerce_settings(ws.settings_json)
    before = dict(current)
    changed_keys: list[str] = []

    for key, raw_value in payload.root.items():
        definition = _CATALOG_BY_KEY.get(key)
        if definition is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_setting", "key": key},
            )
        if raw_value is None:
            if key in current:
                current.pop(key)
                changed_keys.append(key)
            continue
        value = _validate_value(definition, raw_value)
        if current.get(key, definition.catalog_default) != value:
            current[key] = value
            changed_keys.append(key)

    if changed_keys:
        ws.settings_json = current
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="workspace",
            entity_id=ws.id,
            action="workspace.settings_updated",
            diff={
                "before": {
                    key: before.get(key, _CATALOG_BY_KEY[key].catalog_default)
                    for key in changed_keys
                },
                "after": {
                    key: current.get(key, _CATALOG_BY_KEY[key].catalog_default)
                    for key in changed_keys
                },
            },
        )
        _publish_workspace_changed(ctx, changed_keys=changed_keys)

    return _workspace_settings_response(ws)


def _get_workspace(session: Session, ctx: WorkspaceContext) -> Workspace:
    with tenant_agnostic():
        ws = session.get(Workspace, ctx.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail={"error": "workspace_not_found"})
    return ws


def _workspace_settings_response(ws: Workspace) -> WorkspaceSettingsResponse:
    settings_json = _coerce_settings(ws.settings_json)
    return WorkspaceSettingsResponse(
        meta=WorkspaceSettingsMeta(
            name=ws.name,
            timezone=ws.default_timezone,
            currency=ws.default_currency,
            country=_country(settings_json),
            default_locale=ws.default_locale,
        ),
        defaults=_merged_defaults(settings_json),
        policy=WorkspaceSettingsPolicy(
            approvals={
                "always_gated": list(_ALWAYS_GATED_ACTIONS),
                "configurable": list(_CONFIGURABLE_APPROVAL_ACTIONS),
            },
            danger_zone=list(_DANGER_ZONE),
        ),
    )


def _merged_defaults(settings_json: Mapping[str, Any]) -> dict[str, Any]:
    defaults = {item.key: item.catalog_default for item in _CATALOG}
    for key, value in settings_json.items():
        if key in _CATALOG_BY_KEY:
            defaults[key] = value
    return defaults


def _coerce_settings(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _country(settings_json: Mapping[str, Any]) -> str:
    value = settings_json.get("workspace.default_country")
    if isinstance(value, str) and value:
        return value
    return "XX"


def _validate_value(definition: SettingDefinitionResponse, raw_value: Any) -> Any:
    if definition.type == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        raise HTTPException(
            status_code=422,
            detail={
                "error": "setting_type_invalid",
                "key": definition.key,
                "expected": "bool",
            },
        )
    if definition.type == "int":
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "setting_type_invalid",
                    "key": definition.key,
                    "expected": "int",
                },
            )
        return raw_value
    if definition.enum_values is not None and raw_value in definition.enum_values:
        return raw_value
    raise HTTPException(
        status_code=422,
        detail={
            "error": "setting_type_invalid",
            "key": definition.key,
            "expected": "enum",
        },
    )


def _publish_workspace_changed(
    ctx: WorkspaceContext, *, changed_keys: list[str]
) -> None:
    now = SystemClock().now()
    default_event_bus.publish(
        WorkspaceChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            changed_keys=tuple(changed_keys),
        )
    )
