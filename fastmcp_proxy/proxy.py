"""Receive MCP calls from an agent and forward them to its configured upstreams.

The proxy is a sidecar: one process serves one agent, mounting the upstreams
named in its config file and re-exposing their tools under a namespace.

Attaching the `x-rail` ticket to an outbound request is the point of the
component and is not implemented here. What this module holds is the path a
request travels, the configuration behind it, and the startup fetch that says
whether a ticket could be obtained at all. What it enforces is the boundary: no
header the agent supplies reaches an upstream.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from fastmcp_proxy.xrail_auth import (
    NoTicketAvailable,
    TicketSource,
    token_fingerprint,
)

DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent / "bridge.yaml"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
log = logging.getLogger("fastmcp_proxy")


class ConfigError(Exception):
    """The configuration cannot be served. Raised rather than exited: this
    module is importable, and a library that calls `sys.exit` takes its caller
    down past every `except Exception` between them. `main()` is where this
    becomes the container's exit code."""


def config_file() -> Path:
    """Where the upstream list is read from, resolved per call.

    Read here rather than bound at import, so the environment a process is given
    is the environment it uses. An empty value — what an unset compose
    interpolation yields — falls back rather than becoming `Path("")`, which is
    the current directory and passes an existence check.
    """
    raw = os.environ.get("RAIL_PROXY_CONFIG_FILE", "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_FILE


def load_servers() -> list[dict[str, Any]]:
    """Read `mcp.servers` from the config file.

    Raises rather than serving a configuration nobody meant: the image bakes
    `RAIL_PROXY_CONFIG_FILE` to a path that holds no file, so a container
    started without a mounted config stops here instead of coming up with no
    upstream and answering every tool list with nothing. `main` turns this into
    exit 2.
    """
    path = config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8") from exc

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    # Each level is checked rather than assumed: a hand-edited file goes wrong
    # in more shapes than an empty one, and `.get` on a list is a traceback
    # where a sentence would do.
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping, not {type(data).__name__}")
    mcp = data.get("mcp") or {}
    if not isinstance(mcp, dict):
        raise ConfigError(f"{path}: `mcp` must be a mapping, not {type(mcp).__name__}")
    entries = mcp.get("servers") or []
    if not isinstance(entries, list):
        raise ConfigError(
            f"{path}: `mcp.servers` must be a list, not {type(entries).__name__}"
        )

    servers = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") and entry.get("url"):
            servers.append(entry)
            continue
        # Announced rather than dropped quietly: `urls:` for `url:` is a typo
        # that otherwise removes an upstream with no record at any log level,
        # and the file's other rejections all say so.
        log.warning(
            "%s: ignoring an entry without both a name and a url: %s",
            path,
            _clip(entry),
        )

    names = [str(e["name"]) for e in servers]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        # The name is the namespace every tool is prefixed with, so two entries
        # sharing one shadow each other and the loser is never called.
        raise ConfigError(
            f"{path}: duplicate upstream name(s): {', '.join(sorted(duplicates))}"
        )

    if not servers:
        raise ConfigError(f"{path} names no upstream with both a name and a url")

    # The transport rejects a url it cannot use by raising ValueError from the
    # mount loop, which is past every handler and lands as a traceback and exit
    # 1. `gateway:8080/mcp` — the packaged example minus its scheme — is the
    # likeliest hand-edit of this file, so it is checked where the rest of the
    # file's mistakes are reported.
    for entry in servers:
        url = str(entry["url"])
        if not url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{path}: upstream {entry['name']!r} has no http:// or https:// "
                f"scheme: {url!r}"
            )
    return servers


#: How this proxy authenticates to Rail Center. `gcp` is a value the platform
#: defines and this component does not implement, so it is refused rather than
#: quietly treated as `none` — which would 401 on every fetch with nothing
#: saying why.
_AUTH_MODES = {"none", "bearer"}


def ticket_settings() -> dict[str, str] | None:
    """Where this proxy's ticket comes from, or None if it has no control plane.

    All three together, or none. `TicketSource` says why the sandbox name is not
    optional.
    """
    url = os.environ.get("RAIL_CENTER_URL", "").strip()
    host = os.environ.get("RAIL_HOST_ID", "").strip()
    sandbox = os.environ.get("RAIL_SANDBOX_NAME", "").strip()
    if not any((url, host, sandbox)):
        return None

    missing = [
        name
        for name, value in (
            ("RAIL_CENTER_URL", url),
            ("RAIL_HOST_ID", host),
            ("RAIL_SANDBOX_NAME", sandbox),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "a Rail Center is partly configured; these are also required: "
            + ", ".join(missing)
        )
    return {"url": url, "host_id": host, "sandbox_name": sandbox}


def auth_token() -> str | None:
    """The bearer token, when the mode asks for one.

    An empty token under `bearer` is refused rather than sent as nothing: an
    unauthenticated request is indistinguishable from a correctly configured one
    against an issuer that does not require a credential, so it would appear to
    work until the issuer started requiring it.
    """
    mode = os.environ.get("RAIL_AUTH_MODE", "").strip().lower() or "none"
    if mode not in _AUTH_MODES:
        raise ConfigError(
            f"RAIL_AUTH_MODE={mode!r} is not one of {', '.join(sorted(_AUTH_MODES))}"
        )
    if mode == "none":
        return None
    token = os.environ.get("RAIL_AUTH_TOKEN", "").strip()
    if not token:
        raise ConfigError("RAIL_AUTH_MODE=bearer requires RAIL_AUTH_TOKEN")
    return token


def max_ticket_lifetime() -> float | None:
    """An operator's ceiling on a ticket's lifetime. Unset by default — see
    `xrail_auth.IMPLAUSIBLE_TICKET_LIFETIME_SEC` for why there is no built-in
    one."""
    return _seconds("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", None)


def allow_insecure_credential() -> bool:
    """Whether to send a credential to Rail Center over plaintext http.

    Off by default. Loopback and https need no override; this is for a
    plaintext, non-loopback issuer — a container reaching
    `http://host.docker.internal:…`, say, where the request crosses a virtual
    network other containers sit on.
    """
    raw = os.environ.get("RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL", "").strip().lower()
    return raw in ("1", "true", "yes")


def build_ticket_source() -> TicketSource | None:
    """The source this proxy fetches its own ticket from, or None."""
    settings = ticket_settings()
    if settings is None:
        return None
    try:
        return TicketSource(
            settings["url"],
            host_id=settings["host_id"],
            sandbox_name=settings["sandbox_name"],
            auth_token=auth_token(),
            timeout_seconds=ticket_timeout(),
            max_lifetime_seconds=max_ticket_lifetime(),
            allow_insecure_credential=allow_insecure_credential(),
        )
    except ValueError as exc:
        # The base-URL checks, the plaintext-credential refusal and the
        # missing-name invariant all arrive as ValueError from the constructor.
        # They are configuration mistakes, so they read as one.
        raise ConfigError(str(exc)) from exc


def bind_address() -> str:
    """The interface to listen on. Empty is unset, as it is for every other
    setting; a padded value would otherwise reach getaddrinfo and die inside
    uvicorn. An address that is wrong rather than merely padded still fails
    there — nothing here validates one."""
    return os.environ.get("RAIL_PROXY_BIND", "").strip() or "0.0.0.0"


def _seconds(name: str, default: float | None) -> float | None:
    """A positive, finite number of seconds from `name`, or `default`.

    Non-finite passes `> 0` and reaches a transport as an OverflowError, past
    every handler. Zero and negative are reported rather than taken silently:
    somebody who asked for a bound should be told they did not get one.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    fallback = "no bound" if default is None else f"{default:g}"
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, fallback)
        return default
    if not math.isfinite(value) or value <= 0:
        log.warning(
            "%s=%r is not a positive number of seconds; using %s",
            name,
            raw,
            fallback,
        )
        return default
    return value


def upstream_timeout() -> float:
    """Seconds to wait on an upstream, for one request and for the handshake.

    Without it a silent upstream costs a tool call the transport's 300-second
    read default, twice over, and an upstream that answers malformedly hangs the
    call indefinitely — the exchange completes and the wait moves to a protocol
    layer with no deadline of its own. An agent has no way to tell either from a
    slow tool.
    """
    return _seconds("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", 30.0)


def ticket_timeout() -> float:
    """Seconds to wait on Rail Center for this proxy's own ticket.

    Its own setting rather than the upstream one, because the wait falls in a
    different place: the startup fetch runs before the listener is bound, so
    this value is how long a Rail Center that does not answer delays the port
    and `/health`. Sharing the upstream's value would mean raising that for a
    slow tool server also lengthened a container's time to first health check,
    which is how a startup delay becomes a crash loop.
    """
    return _seconds("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", 10.0)


def _clip(value: object, limit: int = 80) -> str:
    """Render a rejected config entry at a bounded length.

    The file is the operator's, so this bounds a log line rather than an attack
    — a hand-edited YAML with a pasted blob in it should not produce a message
    nobody can read.

    Redacted before it is cut, not after — `_USERINFO` needs the closing `@`,
    and a url password is comfortably long enough to be truncated across it.
    """
    text = _redact(repr(value))
    return text if len(text) <= limit else text[:limit] + "…"


#: `scheme://` then everything up to the last `@` of an authority — greedy, so
#: a password containing its own `@` is taken whole rather than left with its
#: tail in the line.
#:
#: **Why this is matched on text rather than on a url object:** the messages
#: that carry a credential are written by libraries that never hand one over.
#: httpx logs `HTTP Request: %s %s …` at INFO, once per request, with the whole
#: url among the arguments — so the leak is at request rate, and redacting only
#: where this module formats a url would miss all of it.
#:
#: The scheme run is length-bounded, and `_redact` returns early on text with no
#: `://` in it at all. `[\w+.-]*://` is quadratic on a string that never
#: satisfies it — every start position tries every length — and this runs on
#: every record, on the loop that serves every mount. A record can carry text an
#: untrusted sandbox chose: the MCP transport logs a rejected `Content-Type`
#: verbatim, and its logger propagates to root. With the run bounded, both
#: quantifiers are, and the match is linear in the length of the line.
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]{0,30}://)[^/\s]*@")


def _redact(text: str) -> str:
    """Remove userinfo from any url in a log message.

    `user:pass@host` is how httpx is told to send Basic auth to an upstream, so
    a credential there is a working configuration rather than a mistake.
    """
    # An early out rather than a guard: with the scheme run bounded the match
    # is already linear, and most records have no url in them at all.
    if "://" not in text:
        return text
    return _USERINFO.sub(r"\g<scheme>***@", text)


class RedactingFilter(logging.Filter):
    """Applies `_redact` to every record, whoever emitted it.

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
        except Exception:
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
        redacted = _redact(text)
        return redacted if redacted != text else value

    @classmethod
    def _redact_record(cls, record: logging.LogRecord) -> None:
        record.msg = cls._redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(cls._redact_value(a) for a in record.args)
        elif isinstance(record.args, Mapping):
            # `logging` accepts a single mapping for `%(name)s` formatting.
            # Rebuilt only if something in it changed, because rebuilding turns
            # a mapping that answers for missing keys into one that raises.
            redacted = {k: cls._redact_value(v) for k, v in record.args.items()}
            if any(redacted[k] is not v for k, v in record.args.items()):
                record.args = redacted

        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        elif isinstance(record.exc_info, tuple):
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _redact(rendered)
            if record.exc_text != rendered:
                # A handler that renders from `exc_info` itself would print the
                # original. Nothing here can sanitise the exception, so the one
                # carrying a credential loses its traceback rather than leaking
                # it; the redacted text stays on the record for the formatters
                # that read it.
                record.exc_info = None


def build_gateway() -> FastMCP:
    """Mount every configured upstream under one endpoint.

    A mount's name becomes the prefix on every tool it re-exposes, so the
    agent sees `<name>_<tool>`.
    """
    gateway = FastMCP(name="datrail-proxy")
    timeout = upstream_timeout()

    for srv in load_servers():
        transport = StreamableHttpTransport(url=srv["url"])
        proxy = create_proxy(
            Client(transport, timeout=timeout, init_timeout=timeout),
            name=f"proxy-{srv['name']}",
        )

        # After `create_proxy`, not before: it mutates the transport it is
        # handed, so setting this first is silently undone. The line reads like
        # ordinary transport configuration and moving it up to the constructor
        # is the natural tidy-up, which is why the order is stated here.
        #
        # `create_proxy` turns on incoming-header forwarding, which is wrong for
        # this component in the one way that matters: the sandbox could set
        # `x-rail` itself and have it arrive upstream unchanged, so the identity
        # the proxy exists to assert would be supplied by the caller it exists
        # to identify. `authorization` rides the same path, re-included by
        # fastmcp rather than stripped. The proxy is the boundary; nothing the
        # agent sends crosses it.
        transport.forward_incoming_headers = False

        gateway.mount(proxy, namespace=srv["name"])
        log.info("mounted '%s' -> %s", srv["name"], srv["url"])

    @gateway.custom_route("/health", methods=["GET"])
    async def health(_request):
        """Liveness only: the process is up and its config parsed.

        It reports nothing about whether a ticket is held. Anything waiting on
        this to decide an agent may start is waiting on the wrong signal.
        """
        return JSONResponse({"status": "ok"})

    return gateway


class McpMethodCompat:
    """Answer everything but POST at the MCP endpoint.

    A server that does not offer the optional server-to-client SSE stream must
    refuse `GET /mcp` with `405 Method Not Allowed`, and a client following the
    transport spec treats anything else as fatal. The stateless app underneath
    already answers 405, so this is not rescuing the handshake — it does three
    smaller things the app does not. `Allow` is narrowed to `POST`, where the
    app advertises `DELETE` as well and has no session to terminate. `/mcp/`
    answers 405 rather than redirecting with a 307 a client is not expecting.
    And the request is answered here rather than routed, on an endpoint an
    untrusted sandbox can reach.
    """

    _RESPONSE = (
        b'{"jsonrpc":"2.0","error":{"code":-32600,'
        b'"message":"This endpoint accepts POST."},"id":null}'
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _answer_here(scope: Scope) -> bool:
        """Anything at the MCP endpoint that is not the one method it serves.

        POST is how an MCP request is made, so it passes through. Every other
        method is answered here rather than forwarded, because the answer is
        the same 405 either way and forwarding one costs work on an endpoint an
        untrusted sandbox can reach.

        There is no session-id exception, because the server is stateless and
        there are no sessions to be carrying an id for. Under a stateful server
        one would belong here: a 404 would then mean the session had ended, and
        answering 405 would leave a client reusing a dead one.

        The path is matched exactly rather than by prefix: `startswith("/mcp")`
        also catches `/mcp-admin` and `/mcpfoo`, and answering those
        `405 Allow: …` asserts something about a route that does not exist.
        """
        return (
            scope["type"] == "http"
            and scope["method"] != "POST"
            and scope["path"] in ("/mcp", "/mcp/")
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._answer_here(scope):
            await self.app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 405,
                "headers": [
                    (b"allow", b"POST"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(self._RESPONSE)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": self._RESPONSE})


def build_app() -> ASGIApp:
    """The ASGI application, wrapped in the compatibility shim.

    Stateless: no session is created, so none can be exhausted. A stateful
    server keeps a transport per session, and nothing reclaims one
    whose client never handshakes — so an unauthenticated caller could retain
    them until the process died, on the one endpoint an untrusted sandbox can
    reach. Nothing is given up: the features the state exists for are the
    server-to-client SSE stream, which the shim above declines outright, and
    resumption, which needs an event store this does not configure.
    """
    return McpMethodCompat(
        build_gateway().http_app(transport="streamable-http", stateless_http=True)
    )


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
    _install_redaction()
    raw = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip()
    if raw and raw.upper() not in _LEVELS and raw.upper() not in _LEVEL_ALIASES:
        log.warning("RAIL_PROXY_LOG_LEVEL=%r is not a level; using INFO", raw)


def _install_redaction() -> None:
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


async def report_ticket(source: TicketSource | None) -> None:
    """Fetch this proxy's ticket once and say what came back.

    The result is logged and nothing else: no request carries the ticket. Doing
    it at startup is what makes a wrong sandbox name or a rejected credential
    something an operator sees while they are watching.

    Never fatal, which is why the source is built by the caller rather than
    here: an issuer that is merely down is a normal condition and the proxy
    serves without a ticket, while a configuration that could not be right is
    refused before anything is served. Building it here would put both outcomes
    behind the same handler and exit 1 on a traceback for the second.
    """
    if source is None:
        log.info("no Rail Center configured — no ticket will be fetched")
        return

    log.info("fetching this proxy's ticket from %s", source.describe())
    try:
        ticket = await source.fetch()
    except NoTicketAvailable as exc:
        log.warning("no ticket held: %s", exc)
        return
    except Exception as exc:
        # Redacted before it is truncated, for the reason `_clip` gives; an
        # `HTTPStatusError` renders the whole url. Truncated rather than
        # clipped, because `_clip` goes through `repr` — the wrong shape for a
        # sentence an operator reads. A timeout's message is empty, so the type
        # has to carry the line alone.
        detail = _redact(str(exc))[:300]
        log.warning(
            "could not fetch a ticket (%s)",
            f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
        )
        return

    now = time.time()
    fingerprint = token_fingerprint(ticket.value)
    if not ticket.is_valid(now):
        # An expired ticket is a well-formed answer, so it arrives here rather
        # than as an error. Reporting it as held would announce a dead identity
        # as a healthy one, in the one log line this fetch exists to produce.
        log.warning(
            "ticket held but already expired (fingerprint=%s, expired %.0fs ago)",
            fingerprint,
            -ticket.remaining(now),
        )
        return
    log.info(
        "ticket held (fingerprint=%s, expires in %.0fs)",
        fingerprint,
        ticket.remaining(now),
    )


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        app = build_app()
        ticket_source = build_ticket_source()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    bind = bind_address()
    raw_port = os.environ.get("RAIL_PROXY_PORT", "").strip() or "8091"
    try:
        port = int(raw_port)
    except ValueError:
        log.error("RAIL_PROXY_PORT is not a number: %r", raw_port)
        return 2
    # Reported rather than left to bind(), which raises OverflowError past every
    # handler. 0 is excluded deliberately: it binds an ephemeral port, so the
    # process comes up somewhere nothing is configured to look.
    if not 1 <= port <= 65535:
        log.error("RAIL_PROXY_PORT is out of range: %d", port)
        return 2

    await report_ticket(ticket_source)

    # uvicorn logs the bind once it has one. Announcing it here would name an
    # address the process may never get.
    # uvicorn installs its own loggers, so `basicConfig` alone leaves the access
    # log and the startup lines at INFO whatever the variable said.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=bind,
            port=port,
            log_level=log_level().lower(),
            # Filtered again below: this constructor runs `dictConfig`, which
            # creates `uvicorn` and `uvicorn.access` with fresh handlers and
            # `propagate = False`, after `configure_logging` has walked
            # everything that existed.
            # No `timeout_graceful_shutdown`: uvicorn's default waits for
            # in-flight requests indefinitely, and a bound here would be the
            # thing that cuts them off. It would not help anyway — every MCP
            # response is server-sent events, and sse_starlette patches
            # uvicorn's exit handler to abort those bodies before any grace
            # applies, so SIGTERM mid-call leaves the agent without a response
            # whatever is set here. Nothing in this process bounds that wait:
            # the upstream timeout governs the call this proxy makes, not the
            # one an agent is making to it. A restart mid-call is the agent's
            # own deadline to survive.
        )
    )
    _install_redaction()
    await server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
