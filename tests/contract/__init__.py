"""Schemathesis contract-test package (cd-3j25).

The suite drives ``schemathesis run`` as a subprocess against a live
copy of the FastAPI app booted by :mod:`scripts.schemathesis_run`.
Custom checks under :mod:`tests.contract.hooks` enforce the three
spec-mandated invariants from
``docs/specs/17-testing-quality.md`` §"API contract":

* ``Authorization: Bearer …`` is present on every non-public path.
* ``Idempotency-Key`` round-trips: a second call with the same key
  returns the cached response (verified only on routes that declare
  the header in their OpenAPI parameters).
* ``ETag`` round-trips on ``GET → If-None-Match → 304`` for any
  response whose schema declares an ``ETag`` header.

The runner is **not** part of the default pytest collection — every
test in this package is gated by the ``schemathesis`` marker
(``pyproject.toml`` ``addopts = -m "not schemathesis"``). Opt in with
``pytest -m schemathesis``; CI runs the runner via a separate job
(`.github/workflows/ci.yml` ``schemathesis``) to keep the unit /
integration jobs fast.
"""
