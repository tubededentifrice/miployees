"""Owner-initiated passkey reset — worker (subject) email template.

Sent by the ``POST /users/{id}/reset_passkey`` HTTP route (cd-y5z3)
to the worker whose passkey is being reset. Carries the real magic
link that walks the worker through the
:func:`app.auth.passkey.register_start` ceremony (purpose
``recover_passkey``) — the same mechanic the self-service recovery
flow uses, but initiated by an owner from the manager UI instead of
by the worker themselves.

The body deliberately calls out the destructive side-effect (every
existing passkey on the account is revoked, every other session
signed out) so the worker understands what will happen when they
click the link, and the inviter's display name so the worker can
sanity-check the action came from a person they expected to act on
their account.

Placeholders:

* ``{display_name}`` — the worker's display name.
* ``{owner_display_name}`` — the owner who initiated the reset; the
  worker page can render "<owner> reset your passkey".
* ``{workspace_name}`` — the workspace the reset was triggered from.
* ``{url}`` — the ``https://<host>/recover/enroll?token=<token>``
  URL the SPA lands the worker on.
* ``{ttl_minutes}`` — TTL string the template echoes so the reader
  knows the window.

See ``docs/specs/03-auth-and-tokens.md`` §"Owner-initiated worker
passkey reset".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — your passkey has been reset"


BODY_TEXT = """\
Hi {display_name},

{owner_display_name} reset your passkey on the {workspace_name}
workspace. To enrol a fresh passkey, open the link below within the
next {ttl_minutes} minutes:

{url}

Important: completing this step revokes every existing passkey on
your account and signs you out of every other active session. Only
use this link on the device you want to keep.

If you weren't expecting this and don't recognise the person who
triggered it, do NOT click the link — reply to this email and ask
your workspace owner to confirm the action.

— crew.day
"""
