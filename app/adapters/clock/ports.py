"""Clock port — re-exported from :mod:`app.util.clock` for uniformity.

Every side-effect seam is reachable as ``app.adapters.<name>.ports``;
this module keeps the pattern consistent for the clock, whose
canonical definition (and :class:`~app.util.clock.SystemClock` /
:class:`~app.util.clock.FrozenClock` implementations) lives in
:mod:`app.util.clock`.
"""

from __future__ import annotations

from app.util.clock import Clock

__all__ = ["Clock"]
