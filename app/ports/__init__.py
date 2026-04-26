"""Cross-context Protocol seams.

Most repository ports live next to their owning context under
``app/domain/<context>/ports.py`` (per
``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 +
the ``project_protocol_seam_layout`` memory). This package is for
**inter-context** seams — places where one bounded context calls
into another bounded context's surface and we deliberately decouple
the caller from the callee's implementation module.

The first such seam is :mod:`app.ports.tasks_create_occurrence` —
the stays context (`app/domain/stays/turnover_generator.py`) needs
to materialise a turnover :class:`Occurrence` row owned by the
tasks context, but the tasks-side service lands in Phase 5 (cd-4qr
follow-up). The Protocol here lets stays compile + ship with a
no-op test double until the real adapter lands; production wiring
in :mod:`app.main` injects the live concretion at startup.

Why a separate package rather than a sibling under
``app/domain/stays/``? Boundary discipline. A port pinned inside
the caller's context invites the caller to grow concrete imports
of the callee's models / services (because they're physically
adjacent in the tree); a port hoisted into ``app/ports/`` makes
the inter-context nature loud — the caller explicitly imports
``app.ports.<seam>`` and the tasks-side concretion in
``app.adapters.<seam>`` (or wherever the live adapter lands).
"""
