"""Host-only ``crewday admin`` overrides."""

from __future__ import annotations

import json

import click
from sqlalchemy.orm import Session

from crewday._main import ConfigError
from crewday._overrides import cli_override

__all__ = ["register"]


@click.command(name="purge")
@click.option("--person", "person_id", required=True, help="User id to purge.")
@click.option(
    "--workspace-id",
    default=None,
    help="Limit purge to one workspace id. Defaults to every workspace for the user.",
)
@click.option("--dry-run", is_flag=True, help="Report target counts without writing.")
@click.pass_obj
def purge(
    _ctx: object,
    *,
    person_id: str,
    workspace_id: str | None,
    dry_run: bool,
) -> None:
    """Anonymise a person and scrub privacy-sensitive dependent rows."""
    try:
        from app.adapters.db.session import make_uow
        from app.domain.privacy import purge_person
    except Exception as exc:
        raise ConfigError(
            "admin purge must run on the server host with app dependencies installed"
        ) from exc

    with make_uow() as session:
        assert isinstance(session, Session)
        result = purge_person(
            session,
            person_id=person_id,
            workspace_id=workspace_id,
            dry_run=dry_run,
        )
    click.echo(
        json.dumps(
            {
                "person_id": result.person_id,
                "workspace_ids": list(result.workspace_ids),
                "anonymized_users": result.anonymized_users,
                "scrubbed_occurrences": result.scrubbed_occurrences,
                "scrubbed_comments": result.scrubbed_comments,
                "scrubbed_expenses": result.scrubbed_expenses,
                "scrubbed_payout_destinations": result.scrubbed_payout_destinations,
                "scrubbed_payslips": result.scrubbed_payslips,
                "deleted_secret_envelopes": result.deleted_secret_envelopes,
                "dry_run": dry_run,
            },
            sort_keys=True,
        )
    )


purge = cli_override("admin", "purge", covers=[])(purge)


def register(root: click.Group) -> None:
    group = root.get_command(click.Context(root), "admin")
    if group is None:
        group = click.Group(name="admin", help="host-only admin commands")
        root.add_command(group)
    if not isinstance(group, click.Group):
        raise RuntimeError("existing 'admin' command is not a group")
    group.add_command(purge)
