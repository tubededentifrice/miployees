"""Owner-initiated passkey reset — owner (notification copy) template.

Sent by the ``POST /users/{id}/reset_passkey`` HTTP route (cd-y5z3)
to the owner who triggered the reset. Spec §03 "Owner-initiated
worker passkey reset" pins this as a **non-consumable notification
copy** — the owner sees a rendered summary of the action with the
worker's email masked, and a "Not you?" forensic link that reports
the action for review. There is no claimable token in this email:
the enrolment ceremony still requires the worker's own mailbox.

Placeholders:

* ``{owner_display_name}`` — the owner's display name (the recipient).
* ``{worker_display_name}`` — the worker whose passkey was reset.
* ``{worker_email_masked}`` — the worker's address with the local
  part collapsed (e.g. ``m***@example.com``); never the plaintext
  address (§15 PII minimisation in audit-adjacent surfaces).
* ``{workspace_name}`` — the workspace the action was triggered from.
* ``{timestamp}`` — ISO-8601 UTC timestamp of the action.
* ``{notice_url}`` — the non-consumable ``/recover/notice`` URL the
  owner lands on if they click anything in the body. The page
  explains "this is your copy; the worker clicks the link in their
  own email" and offers a "Not you?" link to report the action.

See ``docs/specs/03-auth-and-tokens.md`` §"Owner-initiated worker
passkey reset".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — passkey reset confirmation"


BODY_TEXT = """\
Hi {owner_display_name},

You reset the passkey for {worker_display_name} ({worker_email_masked})
on the {workspace_name} workspace at {timestamp}. A magic link has
been mailed to them; clicking the link in their inbox enrols a fresh
passkey under their account and revokes every existing one.

This message is a confirmation copy. The link below is NOT a
claimable enrolment URL — it lands on a notice page so you can
review the action:

{notice_url}

Not you? If you did not trigger this reset, follow the link above
and report the action immediately. The worker has not yet completed
the enrolment, so the account is still recoverable.

— crew.day
"""
