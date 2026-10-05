"""How every interface logs: one format, one level setting, and redaction.

`RedactingFilter` goes on every handler in the process, so a credential in a
url never reaches a log line whichever library wrote it.
"""

from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Mapping

from proxy.core.xrail_auth import redact_credentials

# The name the standalone proxy has always logged under, kept so its output is
# unchanged by the move; renaming it is a change of its own.
log = logging.getLogger("fastmcp_proxy")

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    """Applies `redact_credentials` to every record, whoever emitted it.

    The template and each argument are redacted **in place, one at a time**, and
    an argument that did not change keeps its original object. Both halves are
    load-bearing, and each is the fix for a way of getting this wrong.

    Rendering an argument is what finds the credential: httpx passes the url as
    an argument and passes it as an `httpx.URL`, whose `str` is the whole thing,
    so a test for `isinstance(str)` walks straight past it. Exceptions arrive
    the same way, in `msg` and in `args` both.

    Replacing the record with one rendered string is what must not happen.
    `uvicorn.logging.AccessFormatter` unpacks `record.args` into exactly five
    values, and an access line *does* carry userinfo whenever a caller asks for
    one — the query string goes in undecoded, so `POST /mcp?cb=https://a:b@x/`
    is enough. Collapse on redaction and the untrusted sandbox can drop its own
    request from the access log, and turn one request into fifteen lines of
    `--- Logging error ---`, by choosing a query.

    A traceback is handled too, because `Formatter.format` builds one from
    `exc_info` *after* every filter has run and appends it to the line — so a
    credential removed from the message is printed in full two lines below.
    Filling `exc_text` is what a stock formatter reads instead of building its
    own; where that is not enough, `exc_info` is dropped, which is the only way
    to stop a handler that renders the traceback itself. That costs a rich
    rendering in exactly the case where the traceback holds a secret.

    **It never raises, and it fails closed.** `Handler.handle` guards `emit`
    and not `filter`, and `Logger.callHandlers` guards neither, so anything
    raised here would surface inside whatever called `log.info` — on a request
    path, in library code this repository does not own; a `%`-format mismatch in
    any dependency is enough. A record that could not be redacted is a record
    that might hold a credential, so it is replaced rather than passed on.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._redact_record(record)
        except Exception:  # noqa: BLE001 - a log filter must never raise
            record.msg = "a log record could not be redacted and was withheld"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True

    @staticmethod
    def _redact_value(value: object) -> object:
        """`value` with any credential removed, or `value` itself unchanged.

        Unchanged means the same object, not an equal one: a formatter may care
        what it was, and `%d` on a string is a broken line. Numbers and `None`
        are returned without being rendered at all, both because they cannot
        hold a url and because rendering an argument here means rendering it
        twice — the formatter does it again a moment later.
        """
        if value is None or isinstance(value, (int, float, complex)):
            return value
        text = value if isinstance(value, str) else str(value)
        redacted = redact_credentials(text)
        return redacted if redacted != text else value

    @classmethod
    def _redact_record(cls, record: logging.LogRecord) -> None:
        record.msg = cls._redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(cls._redact_value(a) for a in record.args)
        elif isinstance(record.args, Mapping):
            # `logging` accepts a single mapping for `%(name)s` formatting.
            # Rebuilt only where something in it changed: rebuilding turns a
            # mapping that answers for missing keys — a `defaultdict`, say —
            # into a plain dict that raises, so this leaves one intact on every
            # record that carried no credential and replaces it on the records
            # that did. The outcome is narrowed to those rather than avoided;
            # nothing in this process passes a mapping at all.
            redacted = {k: cls._redact_value(v) for k, v in record.args.items()}
            if any(redacted[k] is not v for k, v in record.args.items()):
                record.args = redacted

        if record.exc_text:
            record.exc_text = redact_credentials(record.exc_text)
        elif isinstance(record.exc_info, tuple):
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_credentials(rendered)
            if record.exc_text != rendered:
                # A handler that renders from `exc_info` itself would print the
                # original. Nothing here can sanitise the exception, so the one
                # carrying a credential loses its traceback rather than leaking
                # it; the redacted text stays on the record for the formatters
                # that read it.
                record.exc_info = None


#: uvicorn resolves a level by dict lookup and raises KeyError on a miss, so its
#: vocabulary is the narrower of the two and the one to validate against.
#: `logging` also accepts WARN, FATAL and NOTSET, which would pass a check
#: against `logging` alone and then kill the server after the mount lines had
#: already been logged — a configuration typo presenting as a startup failure.
_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"}
_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}


def log_level() -> str:
    """The configured level, or INFO. Pure: it neither warns nor configures.

    Empty is unset, as it is for the config path and the port.
    """
    level = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip().upper() or "INFO"
    level = _LEVEL_ALIASES.get(level, level)
    return level if level in _LEVELS else "INFO"


def configure_logging() -> None:
    """Apply RAIL_PROXY_LOG_LEVEL, falling back rather than refusing to start.

    An unusable level is worth a complaint, not an exit: the proxy's job does
    not depend on it, and dying over a log setting loses the traffic too.
    """
    resolved = log_level()
    # TRACE is uvicorn's alone; `logging` has no such level, so the root logger
    # takes the most verbose one it knows.
    logging.basicConfig(
        level="DEBUG" if resolved == "TRACE" else resolved, format=_LOG_FORMAT
    )
    install_redaction()
    raw = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip()
    if raw and raw.upper() not in _LEVELS and raw.upper() not in _LEVEL_ALIASES:
        log.warning("RAIL_PROXY_LOG_LEVEL=%r is not a level; using INFO", raw)


def install_redaction() -> None:
    """Put a `RedactingFilter` on every handler in the process.

    On handlers rather than on loggers, because a filter on a logger does not
    see records propagated up from its children — and httpx, whose INFO line
    carries the whole url once per request, is a child.

    Every logger and not only the root, because a library that configures its
    own handler and sets `propagate = False` never reaches root at all. fastmcp
    does exactly that at import, and its aggregate provider logs an upstream's
    `HTTPStatusError` — url, credential and all — whenever a `tools/list` the
    agent asked for fails.

    Safe to call twice, which `main` does — a handler that already carries one
    is left alone.
    """
    loggers = [logging.root, *logging.root.manager.loggerDict.values()]
    for logger in loggers:
        for handler in getattr(logger, "handlers", []):
            # `addFilter` dedupes by equality and this class defines none, so
            # without the check a second call — or a handler two loggers share
            # — would stack a second instance.
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
