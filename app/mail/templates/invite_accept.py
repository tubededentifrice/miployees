"""Invite-accept email template (§03 "Additional users").

Sent by :func:`app.domain.identity.membership.invite` with the
magic-link token of purpose ``grant_invite`` (24-hour TTL). The
generic magic-link template already covers the wire — this module
exists so the invite surface can carry its own subject line and
workspace / inviter context without widening the catch-all template
with per-purpose conditionals.

Placeholders:

* ``{workspace_name}`` — the workspace display name the invitee is
  being asked to join ("the Sunshine Villas crew").
* ``{inviter_display_name}`` — the human who clicked Invite.
* ``{invitee_display_name}`` — the name the inviter typed into the
  form; echoed back so the recipient can verify the invite was
  meant for them.
* ``{url}`` — the acceptance URL, e.g.
  ``https://crew.day/auth/magic/<token>``. The URL is the same
  ``/auth/magic/{token}`` shape every magic-link uses; the router
  recognises the ``grant_invite`` purpose and forwards to
  ``/invite/accept``.
* ``{ttl_hours}`` — integer-as-string TTL in hours (always ``"24"``
  on the happy path; the ``{ttl_hours}`` slot keeps the copy honest
  if the cap is ever reduced).

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)".
"""

from __future__ import annotations

__all__ = ["BODY_TEXT", "SUBJECT"]


SUBJECT = "crew.day — {inviter_display_name} invited you to {workspace_name}"


# Deliberately plain text — same rationale as the sibling
# :mod:`app.mail.templates.magic_link` template. A Jinja-rendered
# HTML variant lands when the wider template system does.
BODY_TEXT = """\
Hi {invitee_display_name},

{inviter_display_name} invited you to join {workspace_name} on
crew.day.

Follow this link within the next {ttl_hours} hours to accept:

{url}

If you weren't expecting this invite, ignore this message — the
link expires on its own and leaves nothing on your account. The
grants listed inside only activate once you click Accept from a
logged-in session.

— crew.day
"""
