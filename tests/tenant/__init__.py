"""Cross-workspace (tenant-isolation) regression tests.

This package lands the §17 "Cross-tenant regression test" matrix — the
four-case suite (HTTP surface, worker jobs, event subscriptions,
repository parity) that proves a caller holding a workspace-``A``
context cannot reach a workspace-``B`` row, envelope, or event.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" and ``docs/specs/15-security-privacy.md`` §"Constant-time
cross-tenant responses", §"Row-level security (RLS)".
"""

from __future__ import annotations
