"""Shared pytest configuration for crewday tests.

The scaffolding (cd-t5xe) now provides per-context unit
``conftest.py`` files, an integration harness under
``tests/integration/conftest.py``, and shared fakes under
``tests/_fakes/``. This top-level module stays thin on purpose —
cross-cutting fixtures would encourage leakage between contexts.

Only fixtures that every context needs belong here. Today that is
two helpers: :func:`allow_propagated_log_capture`, which compensates
for alembic's ``fileConfig`` side effect on named loggers; and
:func:`_isolate_root_logger_handlers`, an autouse fixture that
restores the root logger's handlers and filters around every test
(see its docstring for the pollution mechanism it guards against).

See ``docs/specs/17-testing-quality.md``.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import socket
from collections.abc import Callable, Iterator
from typing import Protocol

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Close sockets third-party test helpers leave for interpreter teardown.

    Docker/testcontainers occasionally leaves a client socket reachable
    until xdist worker shutdown. Closing any still-open sockets after the
    suite has finished prevents raw ``ResourceWarning`` noise without
    affecting test execution.
    """
    del session, exitstatus
    for obj in gc.get_objects():
        if isinstance(obj, socket.socket) and obj.fileno() != -1:
            with contextlib.suppress(OSError):
                obj.close()


class _SupportsLogFilter(Protocol):
    """Structural shape mirroring stdlib's private ``logging._SupportsFilter``.

    The :mod:`logging` typeshed stub types :attr:`Filterer.filters` as
    ``list[_FilterType]`` where ``_FilterType`` is the union below.
    The alias is not re-exported, so we redeclare it here so the
    snapshot dict carries the same shape the stdlib publishes.
    """

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord: ...


type _FilterEntry = (
    logging.Filter
    | Callable[[logging.LogRecord], bool | logging.LogRecord]
    | _SupportsLogFilter
)


@pytest.fixture
def allow_propagated_log_capture() -> Iterator[Callable[..., None]]:
    """Enable ``caplog`` capture for named loggers across a polluted session.

    The integration fixture's ``alembic upgrade head`` path runs
    :func:`logging.config.fileConfig` with the ``alembic.ini`` default
    ``disable_existing_loggers=True``, which flips ``propagate=False``
    and ``disabled=True`` on every logger not listed in the config.
    Pytest's ``caplog`` fixture attaches its handler to the root
    logger, so records emitted on non-propagating child loggers
    never reach the capture — the test then asserts an empty
    :attr:`~_pytest.logging.LogCaptureFixture.records` list even
    though the behaviour under test worked correctly.

    Call the yielded enabler once per logger name that the test
    expects to capture; the fixture records the prior
    ``propagate`` / ``disabled`` values and restores them during
    teardown so no state leaks into the next test. Variadic —
    enable several loggers in one call when a test captures from
    more than one namespace.

    Usage::

        def test_something(allow_propagated_log_capture, caplog):
            allow_propagated_log_capture("app.api.factory")
            with caplog.at_level(logging.WARNING, logger="app.api.factory"):
                ...

    Or as an autouse class fixture::

        @pytest.fixture(autouse=True)
        def _allow_capture(self, allow_propagated_log_capture):
            allow_propagated_log_capture("app.authz.enforce")

    See also: cd-0dyv (root cause), cd-szxw (follow-up 4th occurrence).
    """
    saved: dict[str, tuple[bool, bool]] = {}

    def enable(*names: str) -> None:
        for name in names:
            log = logging.getLogger(name)
            # Record the ORIGINAL state the first time we touch a
            # given logger so repeat ``enable`` calls inside the
            # same test don't overwrite the pre-test snapshot.
            if name not in saved:
                saved[name] = (log.propagate, log.disabled)
            log.propagate = True
            log.disabled = False

    try:
        yield enable
    finally:
        for name, (propagate, disabled) in saved.items():
            log = logging.getLogger(name)
            log.propagate = propagate
            log.disabled = disabled


@pytest.fixture(autouse=True)
def _isolate_root_logger_handlers() -> Iterator[None]:
    """Snapshot + restore root-logger handlers / filters around every test.

    :func:`app.api.factory.create_app` calls
    :func:`app.util.logging.setup_logging`, which installs a
    :class:`~app.util.logging.RedactionFilter` on a
    :class:`~app.util.logging._CrewdayJsonHandler` attached to the
    root logger. The filter mutates ``record.msg`` *in place* via
    :func:`~app.util.redact.scrub_string` so credential blobs in log
    output are scrubbed before the JSON formatter sees them. That
    write happens once per record and is visible to every other
    handler attached to the root logger — including pytest's
    :class:`caplog` handler, which then captures the post-redaction
    text.

    Tests that build the real app (factory tests, OpenAPI smoke
    tests, ``test_main`` re-export checks, ...) leave the
    ``_CrewdayJsonHandler`` + filter on the root logger after the
    test returns. Subsequent tests that emit a log record with a
    credential-shaped argument (e.g.
    ``tests/unit/auth/test_passkey_login.py::TestAutoRevokeCredentialFreshUow::test_db_failure_is_swallowed_without_raising``)
    then see ``<redacted:credential>`` in their captured records
    instead of the original token, breaking ``caplog`` assertions.

    The fix is to scope the side effect: snapshot the root logger's
    handlers and filters before each test, and restore both lists
    after. Using ``list(...)`` makes shallow copies so re-attaching
    a handler that a test removed (or vice versa) is straightforward.
    The handler list, the filter list on each handler, and the root
    logger's own filter list are all captured. Levels are restored
    too so a test that bumps ``root.setLevel("DEBUG")`` does not
    leak verbosity into the next test.

    See cd-4qvy for the original failure trace.
    """
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = list(root.handlers)
    saved_root_filters = list(root.filters)
    saved_handler_filters: dict[logging.Handler, list[_FilterEntry]] = {
        handler: list(handler.filters) for handler in saved_root_handlers
    }

    try:
        yield
    finally:
        # Restore the handler list, dropping any handler the test
        # added that was not present pre-test. We do not call
        # ``handler.close()`` on the rejects: the test owns those
        # handlers and should have torn them down itself; closing
        # blindly here would mask test bugs.
        current_handlers = list(root.handlers)
        for handler in current_handlers:
            if handler not in saved_root_handlers:
                root.removeHandler(handler)
        for handler in saved_root_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)

        # Restore each surviving handler's filter list. A test that
        # added or removed filters on an existing handler should not
        # leak the change into the next test.
        for handler, original_filters in saved_handler_filters.items():
            handler.filters = list(original_filters)

        # Restore root-level filters (rare, but symmetric).
        root.filters = list(saved_root_filters)

        root.setLevel(saved_root_level)
