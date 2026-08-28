"""Receive MCP calls from an agent and forward them to its configured upstreams.

The proxy is a sidecar: one process serves one agent, mounting the upstreams
named in its config file and re-exposing their tools under a namespace.

Attaching the `x-rail` ticket is the point of the component and is not
implemented here — this module is the path a request travels. What it does
enforce is the boundary that makes attaching one meaningful later: no header
the agent supplies reaches an upstream.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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


def bind_address() -> str:
    """The interface to listen on. Empty is unset, as it is for every other
    setting; a padded value would otherwise reach getaddrinfo and die inside
    uvicorn. An address that is wrong rather than merely padded still fails
    there — nothing here validates one."""
    return os.environ.get("RAIL_PROXY_BIND", "").strip() or "0.0.0.0"


def upstream_timeout() -> float:
    """Seconds to wait on an upstream, for one request and for the handshake.

    Without it a silent upstream costs a tool call the transport's 300-second
    read default, twice over, and an upstream that answers malformedly hangs the
    call indefinitely — the exchange completes and the wait moves to a protocol
    layer with no deadline of its own. An agent has no way to tell either from a
    slow tool.
    """
    raw = os.environ.get("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        log.warning("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS=%r is not a number", raw)
        return 30.0
    # Non-finite passes `> 0` and reaches the transport as an OverflowError,
    # past every handler. Zero and negative are reported rather than taken
    # silently: a timeout of none is the state this setting exists to prevent,
    # so someone who asked for one should be told they did not get it.
    if not math.isfinite(value) or value <= 0:
        log.warning(
            "RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS=%r is not a positive number of "
            "seconds; using 30",
            raw,
        )
        return 30.0
    return value


def _clip(value: object, limit: int = 80) -> str:
    """Render a rejected entry without letting a hostile config set the size of
    the message it produces."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


#: `scheme://` then anything up to the last `@` of an authority. Matched on the
#: rendered message rather than on a url object, because the messages that carry
#: a credential are written by libraries that never hand one over — httpx logs
#: `HTTP Request: POST <url>` at INFO, once per request.
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)[^/\s@]*@")


def _redact(text: str) -> str:
    """Remove userinfo from any url in a log message.

    `user:pass@host` is how httpx is told to send Basic auth to an upstream, so
    a credential there is a working configuration rather than a mistake.
    Redacting only where this module formats a url is not enough: httpx logs the
    full url on every request, so one tool call put the password in the log
    sixteen times.
    """
    return _USERINFO.sub(r"\g<scheme>***@", text)


class RedactingFilter(logging.Filter):
    """Applies `_redact` to every record, whoever emitted it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = ()
        return True


def build_gateway() -> FastMCP:
    """Mount every configured upstream under one endpoint.

    A mount's name becomes the prefix on every tool it re-exposes, so the
    agent sees `<name>_<tool>`. Renaming a server renames every tool the
    agents were prompted with.
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

        It reports nothing about whether a ticket is held, because no ticket
        is fetched here. Anything waiting on this to decide an agent may start
        is waiting on the wrong signal.
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

    Stateless, which is the honest declaration of what this already was. A
    stateful server keeps a transport per session, and nothing reclaims one
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
    # On the handlers rather than on a logger: a filter on a logger does not see
    # records propagated from its children, and httpx is the one that leaks.
    for handler in logging.root.handlers:
        handler.addFilter(RedactingFilter())
    raw = os.environ.get("RAIL_PROXY_LOG_LEVEL", "").strip()
    if raw and raw.upper() not in _LEVELS and raw.upper() not in _LEVEL_ALIASES:
        log.warning("RAIL_PROXY_LOG_LEVEL=%r is not a level; using INFO", raw)


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        app = build_app()
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

    # uvicorn logs the bind once it has one. Announcing it here would name an
    # address the process may never get.
    # uvicorn installs its own loggers, so `basicConfig` alone leaves the access
    # log and the startup lines at INFO whatever the variable said.
    await uvicorn.Server(
        uvicorn.Config(
            app,
            host=bind,
            port=port,
            log_level=log_level().lower(),
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
    ).serve()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
