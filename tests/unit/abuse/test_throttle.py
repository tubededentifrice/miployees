"""Unit tests for :mod:`app.abuse.throttle`.

Two layers:

* :class:`ShieldStore` — sliding-window semantics, scoping,
  thread-safety, window eviction, refusal does not advance window.
* :func:`throttle` decorator — ``key_fn`` derivation, the refusal
  raises ``HTTPException(429, {"error": "rate_limited"})``, wrapping
  preserves the handler's call shape, explicit ``store=`` isolates
  per-test state.

The tests use :class:`app.util.clock.FrozenClock` rather than sleeping
so the window boundaries are deterministic under load.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.abuse.throttle import ShieldStore, throttle
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ShieldStore
# ---------------------------------------------------------------------------


class TestShieldStoreSlidingWindow:
    """Hit-counter semantics of :class:`ShieldStore`.check_and_record`."""

    def test_under_limit_records_and_returns_true(self) -> None:
        store = ShieldStore()
        for _ in range(5):
            assert (
                store.check_and_record(
                    scope="s",
                    key="ip1",
                    limit=5,
                    window=timedelta(seconds=60),
                    now=_PINNED,
                )
                is True
            )

    def test_at_limit_refuses(self) -> None:
        """The Nth accept fills the bucket; the N+1th refuses."""
        store = ShieldStore()
        window = timedelta(seconds=60)
        for _ in range(10):
            assert (
                store.check_and_record(
                    scope="s", key="k", limit=10, window=window, now=_PINNED
                )
                is True
            )
        # 11th hit inside the window is refused.
        assert (
            store.check_and_record(
                scope="s", key="k", limit=10, window=window, now=_PINNED
            )
            is False
        )

    def test_refusal_does_not_advance_window(self) -> None:
        """A refused hit must NOT be recorded — otherwise a pounding
        attacker would push the window boundary forward indefinitely."""
        store = ShieldStore()
        window = timedelta(seconds=60)
        # Fill the bucket.
        for _ in range(3):
            store.check_and_record(
                scope="s", key="k", limit=3, window=window, now=_PINNED
            )
        # A spray at the cap: every extra hit still returns False, and
        # the bucket size never grows past the cap.
        for _ in range(100):
            assert (
                store.check_and_record(
                    scope="s", key="k", limit=3, window=window, now=_PINNED
                )
                is False
            )
        # Advance time just past the window; the original 3 hits
        # evict, so a fresh burst of 3 is accepted.
        later = _PINNED + timedelta(seconds=61)
        for _ in range(3):
            assert (
                store.check_and_record(
                    scope="s", key="k", limit=3, window=window, now=later
                )
                is True
            )
        # And the 4th in the new window is refused again.
        assert (
            store.check_and_record(
                scope="s", key="k", limit=3, window=window, now=later
            )
            is False
        )

    def test_window_slides_rather_than_resets(self) -> None:
        """Old hits fall off individually, not all at once."""
        store = ShieldStore()
        window = timedelta(seconds=60)
        # One hit at t0, another at t+30s.
        store.check_and_record(scope="s", key="k", limit=2, window=window, now=_PINNED)
        store.check_and_record(
            scope="s",
            key="k",
            limit=2,
            window=window,
            now=_PINNED + timedelta(seconds=30),
        )
        # At t+61s the first hit has evicted but the second (at t+30s)
        # is still live — a new hit should fit (1 live + 1 new = 2).
        t_61 = _PINNED + timedelta(seconds=61)
        assert (
            store.check_and_record(scope="s", key="k", limit=2, window=window, now=t_61)
            is True
        )
        # One more at t+62s — now 3 live, over the limit.
        assert (
            store.check_and_record(
                scope="s",
                key="k",
                limit=2,
                window=window,
                now=_PINNED + timedelta(seconds=62),
            )
            is False
        )


class TestShieldStoreScoping:
    """Two scopes never collide on the same key."""

    def test_distinct_scopes_have_independent_buckets(self) -> None:
        store = ShieldStore()
        window = timedelta(seconds=60)
        # Fill the ``a`` scope.
        for _ in range(3):
            store.check_and_record(
                scope="a", key="ip", limit=3, window=window, now=_PINNED
            )
        # ``a/ip`` is full, but ``b/ip`` is still fresh.
        assert (
            store.check_and_record(
                scope="a", key="ip", limit=3, window=window, now=_PINNED
            )
            is False
        )
        assert (
            store.check_and_record(
                scope="b", key="ip", limit=3, window=window, now=_PINNED
            )
            is True
        )

    def test_distinct_keys_under_same_scope_are_independent(self) -> None:
        """Per-IP means per-IP: IP A full does not affect IP B."""
        store = ShieldStore()
        window = timedelta(seconds=60)
        for _ in range(3):
            store.check_and_record(
                scope="s", key="A", limit=3, window=window, now=_PINNED
            )
        assert (
            store.check_and_record(
                scope="s", key="A", limit=3, window=window, now=_PINNED
            )
            is False
        )
        # IP B has its own budget.
        assert (
            store.check_and_record(
                scope="s", key="B", limit=3, window=window, now=_PINNED
            )
            is True
        )


class TestShieldStoreConcurrency:
    """Hammer the store from many threads; total records must equal accepts."""

    def test_limit_is_respected_under_concurrent_hits(self) -> None:
        """10 threads x 5 hits = 50 attempts; limit 20 -> exactly 20 accepts."""
        store = ShieldStore()
        window = timedelta(seconds=60)
        accepts: list[bool] = []
        accepts_lock = threading.Lock()

        def run() -> None:
            local: list[bool] = []
            for _ in range(5):
                ok = store.check_and_record(
                    scope="s", key="k", limit=20, window=window, now=_PINNED
                )
                local.append(ok)
            with accepts_lock:
                accepts.extend(local)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(accepts) == 20, (
            f"expected exactly 20 accepts under a cap of 20, got {sum(accepts)}"
        )
        assert len(accepts) == 50

    def test_clear_drops_every_bucket(self) -> None:
        store = ShieldStore()
        window = timedelta(seconds=60)
        for _ in range(3):
            store.check_and_record(
                scope="s", key="k", limit=3, window=window, now=_PINNED
            )
        # Full bucket ⇒ refuse.
        assert (
            store.check_and_record(
                scope="s", key="k", limit=3, window=window, now=_PINNED
            )
            is False
        )
        store.clear()
        # After clear() the bucket accepts again.
        assert (
            store.check_and_record(
                scope="s", key="k", limit=3, window=window, now=_PINNED
            )
            is True
        )


# ---------------------------------------------------------------------------
# @throttle decorator
# ---------------------------------------------------------------------------


class TestThrottleDecorator:
    """Composition of the decorator with a fake handler."""

    def test_wraps_handler_and_forwards_result(self) -> None:
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="test",
            key_fn=lambda *a, **kw: "k",
            limit=2,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler(n: int) -> int:
            return n * 2

        assert handler(3) == 6
        assert handler(4) == 8

    def test_third_call_raises_429(self) -> None:
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="t",
            key_fn=lambda *a, **kw: "k",
            limit=2,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler() -> str:
            return "ok"

        assert handler() == "ok"
        assert handler() == "ok"
        with pytest.raises(HTTPException) as exc_info:
            handler()
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail == {"error": "rate_limited"}

    def test_key_fn_derives_distinct_buckets(self) -> None:
        """Two callers with different keys each get their own budget."""
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="t",
            key_fn=lambda ip, **_: ip,
            limit=1,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler(ip: str) -> str:
            return f"served {ip}"

        assert handler("ipA") == "served ipA"
        # IP A already used its single budget, but IP B is untouched.
        assert handler("ipB") == "served ipB"
        with pytest.raises(HTTPException):
            handler("ipA")
        with pytest.raises(HTTPException):
            handler("ipB")

    def test_window_slide_reopens_budget(self) -> None:
        """After ``window_s`` elapses, old hits evict and the handler
        is callable again."""
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="t",
            key_fn=lambda *a, **kw: "k",
            limit=1,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler() -> str:
            return "ok"

        assert handler() == "ok"
        with pytest.raises(HTTPException):
            handler()
        # Advance clock past the window.
        clock.advance(timedelta(seconds=61))
        assert handler() == "ok"

    def test_refusal_is_before_handler_body(self) -> None:
        """When the bucket is over, the handler must NOT execute."""
        calls: list[int] = []
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="t",
            key_fn=lambda *a, **kw: "k",
            limit=1,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler() -> None:
            calls.append(1)

        handler()
        with pytest.raises(HTTPException):
            handler()
        assert calls == [1], "handler body must not run when over budget"

    def test_zero_limit_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            throttle(
                scope="t",
                key_fn=lambda *a, **kw: "k",
                limit=0,
                window_s=60,
            )

    def test_negative_window_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            throttle(
                scope="t",
                key_fn=lambda *a, **kw: "k",
                limit=5,
                window_s=-1,
            )

    def test_default_store_is_singleton(self) -> None:
        """Two decorators built without an explicit ``store=`` share the
        module-level default, so their buckets see each other. This is
        the production shape (one worker pool per process)."""
        # Import the *submodule* (not the ``throttle`` function that the
        # package re-exports at :mod:`app.abuse`) so we can poke at the
        # module-level ``_DEFAULT_STORE`` singleton for test-setup only.
        import importlib

        throttle_mod = importlib.import_module("app.abuse.throttle")

        # Clear shared state so a sibling test hasn't filled the
        # module default bucket first.
        throttle_mod._DEFAULT_STORE.clear()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="shared",
            key_fn=lambda *a, **kw: "shared-key",
            limit=2,
            window_s=60,
            clock=clock,
        )
        def first() -> str:
            return "a"

        @throttle(
            scope="shared",
            key_fn=lambda *a, **kw: "shared-key",
            limit=2,
            window_s=60,
            clock=clock,
        )
        def second() -> str:
            return "b"

        first()
        second()
        # Third call (across either decorator) should trip the cap.
        with pytest.raises(HTTPException):
            first()

    def test_explicit_store_isolates(self) -> None:
        """Two decorators with their own ``store=`` never collide."""
        store_a = ShieldStore()
        store_b = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="s",
            key_fn=lambda *a, **kw: "k",
            limit=1,
            window_s=60,
            store=store_a,
            clock=clock,
        )
        def a() -> str:
            return "a"

        @throttle(
            scope="s",
            key_fn=lambda *a, **kw: "k",
            limit=1,
            window_s=60,
            store=store_b,
            clock=clock,
        )
        def b() -> str:
            return "b"

        a()
        b()
        # Each store is independent.
        with pytest.raises(HTTPException):
            a()
        with pytest.raises(HTTPException):
            b()

    def test_wraps_preserves_function_identity(self) -> None:
        """``functools.wraps`` carries ``__name__`` + ``__wrapped__`` so
        FastAPI's signature inspection sees through the decorator."""

        @throttle(
            scope="t",
            key_fn=lambda *a, **kw: "k",
            limit=1,
            window_s=60,
            store=ShieldStore(),
        )
        def my_handler(request: object) -> object:
            return request

        assert my_handler.__name__ == "my_handler"
        assert getattr(my_handler, "__wrapped__", None) is not None

    def test_keyword_arguments_reach_key_fn(self) -> None:
        """FastAPI binds handler args by keyword; the key_fn must see them."""
        store = ShieldStore()
        clock = FrozenClock(_PINNED)

        @throttle(
            scope="t",
            key_fn=lambda *a, **kw: str(kw.get("ip", "")),
            limit=1,
            window_s=60,
            store=store,
            clock=clock,
        )
        def handler(ip: str) -> str:
            return ip

        assert handler(ip="A") == "A"
        # Same IP ⇒ cap tripped.
        with pytest.raises(HTTPException):
            handler(ip="A")
        # Distinct IP has its own budget.
        assert handler(ip="B") == "B"
