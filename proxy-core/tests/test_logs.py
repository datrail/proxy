"""How every interface logs: the level setting, the format, and redaction.

`RedactingFilter` is the last thing between a credential in a url and a log
line, so most of what is here is about the shapes a record can arrive in.
"""

import logging
import sys

import pytest

from proxy.core import logs


@pytest.mark.parametrize(
    ("level", "effective"),
    [("debug", 10), ("WARNING", 30), ("not-a-level", 20)],
    ids=["lowercase", "exact", "unusable-falls-back"],
)
def test_the_log_level_is_applied_or_fallen_back_from(monkeypatch, level, effective):
    """An unusable level is a complaint, not an exit: the proxy's job does not
    depend on it, and dying over a log setting loses the traffic too."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", level)
    monkeypatch.setattr(logging.root, "handlers", [])

    logs.configure_logging()

    assert logging.root.level == effective


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("WARN", "WARNING"),
        ("FATAL", "CRITICAL"),
        ("NOTSET", "INFO"),
        ("TRACE", "TRACE"),
        ("debug", "DEBUG"),
        ("", "INFO"),
        ("not-a-level", "INFO"),
    ],
    ids=["warn-alias", "fatal-alias", "notset", "trace", "lowercase", "empty", "junk"],
)
def test_the_level_resolves_to_one_uvicorn_accepts(monkeypatch, value, expected):
    """uvicorn's vocabulary is the narrower one, and it resolves a level by dict
    lookup — a miss is a KeyError after startup has begun."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", value)

    assert logs.log_level() == expected


@pytest.mark.parametrize(
    ("value", "warns"),
    [("WARNING", False), ("WARN", False), ("", False), ("not-a-level", True)],
    ids=["exact", "alias", "unset", "junk"],
)
def test_only_an_unusable_level_is_complained_about(monkeypatch, caplog, value, warns):
    """An alias and an unset value are both handled rather than wrong, so
    neither should produce a line an operator has to read past."""
    # Root's handlers are left alone: caplog captures through one, so clearing
    # them removes the very thing this asserts against.
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", value)

    with caplog.at_level("WARNING"):
        logs.configure_logging()

    assert any("is not a level" in r.message for r in caplog.records) is warns


def test_trace_is_translated_for_the_root_logger(monkeypatch):
    """TRACE is uvicorn's alone. `logging.basicConfig(level="TRACE")` raises
    ValueError, which would escape configure_logging before main's ConfigError
    handler — a crash-loop from a log setting. Nothing else calls
    configure_logging with it."""
    monkeypatch.setenv("RAIL_PROXY_LOG_LEVEL", "TRACE")
    monkeypatch.setattr(logging.root, "handlers", [])

    logs.configure_logging()

    assert logging.root.level == logging.DEBUG


def test_the_filter_rewrites_a_record_rather_than_only_passing_it(monkeypatch, caplog):
    """A filter that is attached and does nothing passes every test that checks
    it is attached. What matters is that httpx's own INFO line — which carries
    the full url, credentials and all, once per request — comes out redacted."""
    import logging

    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    logs.configure_logging()
    handler = caplog.handler
    handler.addFilter(logs.RedactingFilter())

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: GET https://svc:hunter2@rc.invalid/v1/tickets",
        None,
        None,
    )
    handler.handle(record)

    assert "hunter2" not in caplog.text
    assert "rc.invalid" in caplog.text


def test_a_log_line_carries_its_level_and_its_logger(monkeypatch, capsys):
    """Bare messages are unreadable in a container's stdout, where this
    process's lines are interleaved with uvicorn's and httpx's."""
    import logging

    monkeypatch.setattr(logging.root, "handlers", [], raising=False)
    logs.configure_logging()
    logging.getLogger("proxy.core.logs").warning("a message")

    err = capsys.readouterr().err
    assert "[WARNING] proxy.core.logs: a message" in err


def test_redaction_reaches_a_logger_that_never_propagates_to_root():
    """A library that configures its own handler and sets `propagate = False`
    never reaches root, so a filter installed only there never sees it. fastmcp
    does exactly that at import, and its aggregate provider logs an upstream's
    `HTTPStatusError` — url, credential and all — every time a `tools/list` the
    agent asked for fails."""
    import logging

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    library = logging.getLogger("some-library-that-configures-itself")
    library.propagate = False
    handler = _Collect()
    library.addHandler(handler)
    try:
        logs.configure_logging()
        library.error(
            "Error during list_tools: %s",
            "Client error '401' for url 'https://svc:hunter2@up.invalid/mcp'",
        )
    finally:
        library.removeHandler(handler)

    assert lines
    assert "hunter2" not in "\n".join(lines)
    assert "https://***@up.invalid/mcp" in "\n".join(lines)


def test_a_traceback_is_redacted_along_with_the_message_above_it():
    """`Formatter.format` builds the traceback from `exc_info` after every
    filter has run and appends it, so a credential removed from the message is
    printed in full two lines below. The MCP transport calls `logger.exception`
    on the request path."""
    import logging

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    logger = logging.getLogger("test-traceback-redaction")
    handler = _Collect()
    handler.addFilter(logs.RedactingFilter())
    logger.addHandler(handler)
    try:
        raise RuntimeError("for url 'https://svc:hunter2@up.invalid/mcp'")
    except RuntimeError:
        logger.exception("Error handling POST request")
    finally:
        logger.removeHandler(handler)

    rendered = "\n".join(lines)
    assert "Traceback (most recent call last)" in rendered
    assert "hunter2" not in rendered
    assert "https://***@up.invalid/mcp" in rendered


def test_the_filter_never_raises_into_the_call_that_logged(monkeypatch):
    """`Handler.handle` guards `emit` and not `filter`, and `callHandlers`
    guards neither — so anything raised here surfaces inside whatever called
    `log.info`, in library code on a request path. This filter sits on every
    handler in the process, so the blast radius is every log call in it."""
    import logging

    monkeypatch.setattr(
        logs,
        "redact_credentials",
        lambda _text: (_ for _ in ()).throw(RuntimeError("redaction broke")),
    )
    emitted: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            emitted.append(record.getMessage())

    logger = logging.getLogger("test-filter-never-raises")
    logger.propagate = False
    handler = _Collect()
    handler.addFilter(logs.RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.error("a url: %s", "https://svc:hunter2@rc.invalid/x")
    finally:
        logger.removeHandler(handler)

    # Withheld rather than dropped or passed on: a record that could not be
    # redacted might be carrying the thing the filter exists to remove.
    assert emitted == ["a log record could not be redacted and was withheld"]


def test_a_traceback_that_holds_a_credential_is_dropped_rather_than_re_rendered():
    """`exc_text` is only read by a formatter that builds its own traceback.
    fastmcp's handler renders from `exc_info` directly, so the record that
    carries a secret loses its traceback — and one that does not, keeps it."""
    import logging

    def record_for(message: str) -> logging.LogRecord:
        try:
            raise RuntimeError(message)
        except RuntimeError:
            return logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
            )

    dirty = record_for("for url 'https://svc:hunter2@up.invalid/mcp'")
    clean = record_for("nothing sensitive here")
    logs.RedactingFilter().filter(dirty)
    logs.RedactingFilter().filter(clean)

    assert dirty.exc_info is None
    assert "hunter2" not in dirty.exc_text
    assert "***@up.invalid" in dirty.exc_text
    assert clean.exc_info is not None

    # And an `exc_text` a previous handler already cached. Filters run per
    # handler, so the second one sees a rendered traceback rather than the
    # `exc_info` it was built from.
    cached = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
    cached.exc_text = "Traceback:\n  for url 'https://svc:hunter2@up.invalid/mcp'"
    logs.RedactingFilter().filter(cached)
    assert "hunter2" not in cached.exc_text
    assert "***@up.invalid" in cached.exc_text


def test_a_credential_that_is_not_a_string_is_still_redacted():
    """Every argument is rendered, because the value carrying the credential is
    usually not a `str`. httpx passes an `httpx.URL`, whose `str` is the whole
    url and whose `repr` masks it; library code passes exceptions. A test for
    `isinstance(str)` walks straight past both."""
    import logging

    import httpx

    for template, args in (
        (
            'HTTP Request: %s %s "%s %d %s"',
            (
                "GET",
                httpx.URL("https://svc:hunter2@rc.invalid/v1/tickets"),
                "HTTP/1.1",
                200,
                "OK",
            ),
        ),
        (
            "boom: %s",
            (RuntimeError("for url 'https://svc:hunter2@rc.invalid/x'"),),
        ),
    ):
        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1, template, args, None
        )
        logs.RedactingFilter().filter(record)

        assert "hunter2" not in record.getMessage(), template
        assert "***@rc.invalid" in record.getMessage(), template


def test_a_record_that_is_an_exception_rather_than_a_template_is_redacted():
    """`log.error(exc)` puts the exception in `msg`, where an `isinstance(str)`
    test does not see it. `main` logs a `ConfigError` this way, and one can
    quote a url from `bridge.yaml`."""
    import logging

    record = logging.LogRecord(
        "x",
        logging.ERROR,
        __file__,
        1,
        RuntimeError("scheme: 'ftp://svc:hunter2@gateway:8080/mcp'"),
        None,
        None,
    )
    logs.RedactingFilter().filter(record)

    assert "hunter2" not in record.getMessage()


def test_a_withheld_record_keeps_no_traceback_either(monkeypatch):
    """The message is replaced because it might hold a credential; the
    traceback it came with might hold the same one."""
    import logging

    monkeypatch.setattr(
        logs,
        "redact_credentials",
        lambda _text: (_ for _ in ()).throw(RuntimeError("redaction broke")),
    )
    try:
        raise RuntimeError("for url 'https://svc:hunter2@rc.invalid/x'")
    except RuntimeError:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
        )
    record.exc_text = "cached: https://svc:hunter2@rc.invalid/x"

    logs.RedactingFilter().filter(record)

    assert record.exc_info is None
    assert record.exc_text is None


def test_a_second_install_does_not_stack_a_second_filter():
    """`main` calls it twice — once before uvicorn builds its loggers and once
    after. `addFilter` dedupes by equality, which this class does not define."""
    import logging

    handler = logging.StreamHandler()
    logging.root.addHandler(handler)
    try:
        logs.install_redaction()
        logs.install_redaction()
        logs.install_redaction()
        installed = [f for f in handler.filters if isinstance(f, logs.RedactingFilter)]
    finally:
        logging.root.removeHandler(handler)

    assert len(installed) == 1


def test_an_argument_that_needs_no_redaction_keeps_its_own_type():
    """Replaced wholesale, a `%d` argument becomes a string and the line
    becomes a `--- Logging error ---`. Only what changed is replaced."""
    import logging

    args = (200, 1.5, None, "plain", b"bytes")
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "%d %s %s %s %s", args, None
    )

    logs.RedactingFilter().filter(record)

    assert record.args == args
    assert all(a is b for a, b in zip(record.args, args))


def test_a_template_that_does_not_match_its_arguments_is_still_redacted():
    """`handleError` prints `Arguments: %s` — the raw tuple — to stderr when the
    formatter fails, so a mismatch is a path where unredacted arguments reach a
    log sink even though no message ever renders."""
    import logging

    record = logging.LogRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "upstream refused the handshake",
        ("https://svc:hunter2@rc.invalid/v1/tickets",),
        None,
    )

    logs.RedactingFilter().filter(record)

    assert record.args == ("https://***@rc.invalid/v1/tickets",)


def test_a_credential_in_a_mapping_argument_is_redacted_too():
    """`log.info("%(url)s", {"url": ...})` is a shape the standard library
    supports and libraries use. The filter's rule is that it applies to every
    record, whoever emitted it — a branch that rebuilds the mapping without
    redacting it keeps the object identity a formatter needs and drops the one
    thing the filter is for."""
    import logging

    record = logging.LogRecord(
        "somelib",
        logging.INFO,
        __file__,
        1,
        "connecting to %(url)s",
        # A one-tuple, which is how `logging` is handed a mapping: LogRecord
        # unwraps it and stores the dict as `args`.
        ({"url": "https://svc:hunter2@rc.invalid/v1/tickets"},),
        None,
    )
    logs.RedactingFilter().filter(record)

    assert "hunter2" not in record.getMessage()
    assert "***@rc.invalid" in record.getMessage()
