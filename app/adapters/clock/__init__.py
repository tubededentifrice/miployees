"""Clock adapter package — re-exports :class:`~app.util.clock.Clock`.

The canonical :class:`Clock` protocol and its implementations
(:class:`~app.util.clock.SystemClock`, :class:`~app.util.clock.FrozenClock`)
live in :mod:`app.util.clock`. This package exists so the adapter
surface is uniform: every side-effect seam is importable from
``app.adapters.<name>.ports``.
"""
