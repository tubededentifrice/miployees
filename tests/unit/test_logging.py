"""Tests for :mod:`app.util.logging`.

Covers setup, JSON formatting, the redaction filter's key- and
value-based rules, correlation-id binding across contextvars, and
threading / asyncio isolation.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr

from app.util.logging import (
    JsonFormatter,
    RedactionFilter,
    reset_correlation_id,
    set_correlation_id,
    setup_logging,
)


@pytest.fixture
def stream() -> io.StringIO:
    """Fresh in-memory stream per test; the root logger writes here."""
    return io.StringIO()


@pytest.fixture
def configured_logger(
    stream: io.StringIO,
) -> Generator[logging.Logger, None, None]:
    """Root logger wired for JSON + redaction into ``stream``.

    We tear down the handler after each test so parallel tests don't
    cross-contaminate, and reset the root level to WARNING so other
    suites aren't affected.
    """
    setup_logging(level="INFO", stream=stream)
    try:
        yield logging.getLogger("crewday.test")
    finally:
        root = logging.getLogger()
        # setup_logging is idempotent; remove our handler explicitly so
        # subsequent tests see a quiet root.
        for handler in list(root.handlers):
            if handler.formatter is not None and isinstance(
                handler.formatter, JsonFormatter
            ):
                root.removeHandler(handler)
        root.setLevel(logging.WARNING)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    """Parse captured output as one JSON object per line."""
    raw = stream.getvalue().strip().splitlines()
    return [json.loads(line) for line in raw if line]


def _as_str(value: object) -> str:
    """Narrow a JSON-decoded value to ``str`` for `in` / substring checks."""
    assert isinstance(value, str), f"expected str, got {type(value).__name__}"
    return value


def _as_list(value: object) -> list[object]:
    """Narrow a JSON-decoded value to ``list`` for indexing."""
    assert isinstance(value, list), f"expected list, got {type(value).__name__}"
    return value


class TestSetupLogging:
    def test_info_level_suppresses_debug(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.debug("should be hidden")
        configured_logger.info("visible")
        lines = _lines(stream)
        assert len(lines) == 1
        assert lines[0]["msg"] == "visible"

    def test_is_idempotent(self, stream: io.StringIO) -> None:
        setup_logging(level="INFO", stream=stream)
        setup_logging(level="INFO", stream=stream)
        logging.getLogger("t").info("once")
        # Only one JSON line, not duplicated.
        assert len(_lines(stream)) == 1
        # Tear down.
        root = logging.getLogger()
        for h in list(root.handlers):
            if h.formatter is not None and isinstance(h.formatter, JsonFormatter):
                root.removeHandler(h)


class TestJsonOutput:
    def test_happy_path_flattens_extra(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("hello", extra={"user": "u1"})
        line = _lines(stream)[0]
        assert line["msg"] == "hello"
        assert line["user"] == "u1"
        assert line["logger"] == "crewday.test"
        assert line["level"] == "INFO"

    def test_level_is_string_not_int(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.warning("warn")
        line = _lines(stream)[0]
        assert line["level"] == "WARNING"
        assert isinstance(line["level"], str)

    def test_time_is_iso8601_utc(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("ping")
        line = _lines(stream)[0]
        time_str = line["time"]
        assert isinstance(time_str, str)
        # Either Z or +00:00; our formatter uses +00:00.
        assert time_str.endswith("+00:00") or time_str.endswith("Z")
        # Must include 'T' separator.
        assert "T" in time_str

    def test_each_line_is_valid_json(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        for i in range(5):
            configured_logger.info("line", extra={"i": i})
        for raw in stream.getvalue().strip().splitlines():
            json.loads(raw)  # raises if malformed

    def test_reserved_key_collision_is_namespaced(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        # ``msg`` / ``exc_info`` are blocked by the stdlib ``extra=``
        # validator (part of :class:`LogRecord.__dict__`), but ``time``,
        # ``level``, ``logger``, ``correlation_id`` are all valid
        # extra keys. Our envelope reserves them — they must be
        # namespaced with a leading underscore when the caller collides.
        configured_logger.info(
            "hi",
            extra={
                "time": "caller-ts",
                "level": "caller-level",
                "logger": "caller-logger",
                "correlation_id": "caller-cid",
            },
        )
        line = _lines(stream)[0]
        assert line["_time"] == "caller-ts"
        assert line["_level"] == "caller-level"
        assert line["_logger"] == "caller-logger"
        assert line["_correlation_id"] == "caller-cid"
        # The envelope's own fields keep their canonical names.
        assert line["logger"] == "crewday.test"
        assert line["level"] == "INFO"


class TestRedactionKeys:
    def test_token_key_is_redacted(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("auth", extra={"token": "abc"})
        line = _lines(stream)[0]
        assert line["token"] == "<redacted:sensitive-key>"

    @pytest.mark.parametrize(
        "key",
        ["Authorization", "AUTHORIZATION", "authorization", "X-Authorization"],
    )
    def test_key_matching_is_case_insensitive(
        self,
        configured_logger: logging.Logger,
        stream: io.StringIO,
        key: str,
    ) -> None:
        configured_logger.info("m", extra={key: "Bearer xyz"})
        line = _lines(stream)[0]
        assert line[key] == "<redacted:sensitive-key>"

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "api-key",
            "cookie",
            "session_id",
            "secret",
            "passkey",
            "credential",
        ],
    )
    def test_all_sensitive_keys_redact(
        self,
        configured_logger: logging.Logger,
        stream: io.StringIO,
        key: str,
    ) -> None:
        configured_logger.info("m", extra={key: "value"})
        line = _lines(stream)[0]
        assert line[key] == "<redacted:sensitive-key>"

    def test_non_sensitive_key_is_preserved(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("m", extra={"user_id": "u1", "count": 7})
        line = _lines(stream)[0]
        assert line["user_id"] == "u1"
        assert line["count"] == 7


class TestRedactionValues:
    def test_bearer_token_in_message(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("auth Bearer xyz123abc")
        msg = _as_str(_lines(stream)[0]["msg"])
        assert "xyz123abc" not in msg
        assert "<redacted:credential>" in msg

    def test_jwt_shape_in_message(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("got jwt eyJhbG.abc.def here")
        msg = _as_str(_lines(stream)[0]["msg"])
        assert "eyJhbG.abc.def" not in msg
        assert "<redacted:credential>" in msg

    def test_64_char_hex_in_message(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        hex_secret = "a" * 32 + "0" * 32  # 64 hex chars
        configured_logger.info(f"token value {hex_secret} end")
        msg = _as_str(_lines(stream)[0]["msg"])
        assert hex_secret not in msg
        assert "<redacted:credential>" in msg

    def test_short_hex_not_redacted(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        # 16 hex chars — below the 32-char threshold.
        configured_logger.info("short deadbeefcafebabe end")
        msg = _as_str(_lines(stream)[0]["msg"])
        assert "deadbeefcafebabe" in msg

    def test_secretstr_value_in_extra(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("m", extra={"value": SecretStr("hunter2")})
        line = _lines(stream)[0]
        # Either redacted by key (value isn't a sensitive key) or by
        # type. The SecretStr type check must fire regardless.
        assert line["value"] == "<redacted:sensitive-key>"
        assert "hunter2" not in stream.getvalue()

    def test_secretstr_under_sensitive_key_still_safe(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("m", extra={"password": SecretStr("hunter2")})
        assert "hunter2" not in stream.getvalue()


class TestNestedRedaction:
    def test_nested_dict_redacts_sensitive_key_at_depth_2(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info(
            "m",
            extra={"payload": {"Authorization": "Bearer abc123"}},
        )
        line = _lines(stream)[0]
        payload = line["payload"]
        assert isinstance(payload, dict)
        assert payload["Authorization"] == "<redacted:sensitive-key>"

    def test_list_values_recurse(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("m", extra={"items": ["plain", "Bearer abc123xyz"]})
        items = _as_list(_lines(stream)[0]["items"])
        assert items[0] == "plain"
        second = _as_str(items[1])
        assert "abc123xyz" not in second
        assert "<redacted:credential>" in second

    def test_deep_nesting_beyond_cap_still_scanned_via_repr(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        # Past the walker's recursion cap the value is repr'd and the
        # string scan still catches the Bearer. The cap is driven by
        # :data:`app.util.redact._MAX_DEPTH`; we nest well past it so
        # the repr fallback is exercised regardless of future bumps.
        nested: object = "Bearer deepnested123"
        for _ in range(20):
            nested = {"x": nested}
        configured_logger.info("m", extra={"a": nested})
        out = stream.getvalue()
        assert "deepnested123" not in out
        assert "<redacted:credential>" in out


class TestExceptionLogging:
    def test_exception_records_traceback(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            configured_logger.exception("caught")
        line = _lines(stream)[0]
        assert "exc_info" in line
        exc = line["exc_info"]
        assert isinstance(exc, str)
        assert "RuntimeError" in exc
        assert "boom" in exc


class TestCorrelationId:
    def test_absent_by_default(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        configured_logger.info("m")
        line = _lines(stream)[0]
        assert "correlation_id" not in line

    def test_bound_value_appears(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        token = set_correlation_id("req-123")
        try:
            configured_logger.info("m")
        finally:
            reset_correlation_id(token)
        line = _lines(stream)[0]
        assert line["correlation_id"] == "req-123"

    def test_reset_unbinds(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        token = set_correlation_id("req-abc")
        reset_correlation_id(token)
        configured_logger.info("m")
        line = _lines(stream)[0]
        assert "correlation_id" not in line

    def test_context_isolates_across_async_tasks(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        """Two concurrent tasks must not leak correlation ids."""

        async def runner() -> None:
            async def task(cid: str, barrier: asyncio.Event) -> None:
                token = set_correlation_id(cid)
                try:
                    # Both tasks hold their id over an await.
                    barrier.set()
                    await asyncio.sleep(0)
                    configured_logger.info(cid)
                finally:
                    reset_correlation_id(token)

            barrier_a = asyncio.Event()
            barrier_b = asyncio.Event()
            await asyncio.gather(
                task("cid-a", barrier_a),
                task("cid-b", barrier_b),
            )

        asyncio.run(runner())
        lines = _lines(stream)
        by_msg = {line["msg"]: line["correlation_id"] for line in lines}
        assert by_msg == {"cid-a": "cid-a", "cid-b": "cid-b"}


class TestThreadedOutput:
    def test_concurrent_logs_produce_valid_json_lines(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        """Every captured line must parse; no interleaved JSON."""

        def emit(i: int) -> None:
            configured_logger.info(f"msg-{i}", extra={"i": i})

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(emit, range(100)))

        raw = stream.getvalue().strip().splitlines()
        assert len(raw) == 100
        for line in raw:
            parsed = json.loads(line)
            assert parsed["msg"].startswith("msg-")

    def test_correlation_id_threading_isolation(
        self, configured_logger: logging.Logger, stream: io.StringIO
    ) -> None:
        """Each thread's correlation id must be invisible to the other."""
        start = threading.Event()

        def worker(cid: str) -> None:
            token = set_correlation_id(cid)
            try:
                start.wait(timeout=1.0)
                configured_logger.info(cid)
            finally:
                reset_correlation_id(token)

        t1 = threading.Thread(target=worker, args=("thread-a",))
        t2 = threading.Thread(target=worker, args=("thread-b",))
        t1.start()
        t2.start()
        start.set()
        t1.join()
        t2.join()

        lines = _lines(stream)
        by_msg = {line["msg"]: line["correlation_id"] for line in lines}
        assert by_msg == {"thread-a": "thread-a", "thread-b": "thread-b"}


class TestFilterUsableStandalone:
    """Direct unit tests on :class:`RedactionFilter` / :class:`JsonFormatter`
    without going through ``setup_logging``."""

    def test_filter_mutates_record_msg(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token Bearer abcdefg123",
            args=None,
            exc_info=None,
        )
        RedactionFilter().filter(record)
        assert "abcdefg123" not in str(record.msg)

    def test_formatter_produces_parseable_line(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        out = JsonFormatter().format(record)
        parsed = json.loads(out)
        assert parsed["msg"] == "hello world"
        assert parsed["level"] == "INFO"


class TestMalformedFormatArgs:
    """A malformed ``logger.info("%(foo)s", {})`` call must not crash the
    logging pipeline — the filter and formatter fall back to the raw
    template so the record still ships."""

    @pytest.mark.parametrize(
        ("msg", "args"),
        [
            # KeyError: mapping args missing the named key.
            ("%(foo)s", ({"bar": 1},)),
            # IndexError: too few positional args for %s placeholders.
            ("%s %s", ("only-one",)),
            # TypeError: wrong type for conversion spec.
            ("%d", ("not-an-int",)),
        ],
    )
    def test_filter_survives_malformed_format(
        self, msg: str, args: tuple[object, ...]
    ) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )
        # Must not raise.
        assert RedactionFilter().filter(record) is True
        # The record still has a ``msg`` string so downstream formatters
        # can serialise it; no exception propagated up.
        assert isinstance(record.msg, str)

    def test_formatter_survives_malformed_format(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%(foo)s",
            args=({"bar": 1},),
            exc_info=None,
        )
        out = JsonFormatter().format(record)
        parsed = json.loads(out)
        # Fallback text is the raw template — no crash, no silent drop.
        assert parsed["msg"] == "%(foo)s"
