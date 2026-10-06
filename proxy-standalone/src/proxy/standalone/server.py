"""Receive MCP calls from an agent and forward them to its configured upstreams.

The proxy is a sidecar: one process serves one agent, mounting the upstreams
named in its config file and re-exposing their tools under a namespace.

What this module holds is the path a request travels, the configuration behind
it, and the wiring that puts this proxy's own `x-rail` ticket on everything it
forwards. Obtaining and holding that ticket is `xrail_auth`'s.

It is also the boundary: no header the agent supplies reaches an upstream.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from proxy.core.lifecycle import build_holder, health_payload, running
from proxy.core.logs import configure_logging, install_redaction, log_level
from proxy.core.settings import (
    ConfigError,
    plugin_enabled,
    refuse_a_credential_in_the_url,
    seconds_setting,
    warn_if_in_the_clear,
)
from proxy.core.xrail_auth import (
    TicketHolder,
    XRailInjector,
    agent_header_may_cross,
    redact_credentials,
)

# Relative to the working directory: /app/standalone/bridge.yaml in the image.
DEFAULT_CONFIG_FILE = Path("standalone/bridge.yaml")
log = logging.getLogger(__name__)


def config_file() -> Path:
    """Where the upstream list is read from, resolved per call.

    Read here rather than bound at import, so the environment a process is given
    is the environment it uses. An empty value — what an unset compose
    interpolation yields — falls back rather than becoming `Path("")`, which is
    the current directory and passes an existence check.
    """
    raw = os.environ.get("RAIL_PROXY_CONFIG_FILE", "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_FILE


#: What an upstream entry reads. Anything else is warned about rather than
#: dropped in silence, and the entry is still served — see the warning in the
#: loop below, which says why a refusal would be the worse outcome.
_SERVER_KEYS = {"name", "url"}


#: The config file's own format version, and it is not the policy bundle's.
#: Rail Center writes the bundle's; a release of this component writes what this
#: file may say. They share the word so an operator learns the concept once, and
#: they will not move together.
#:
#: Compared on the major alone: a minor bump is a compatible addition, so an
#: older proxy reads the file and serves it, ignoring whatever that minor added.
#: It says so only where the addition lands inside an upstream entry, which is
#: the one place an unknown key is warned about; a new top-level section or a
#: new key under `mcp` arrives unremarked. A major it does not know is a file
#: written for a different reader, and it refuses to start rather than serving
#: the parts it recognises.
_CONFIG_SCHEMA_MAJOR = 1


def _check_schema_version(path: Path, raw: Any) -> None:
    """Refuse a config file this proxy cannot read whole.

    Absent is read as `1.0` and warned about, because every file written before
    the field existed omits it and stopping those is a cost with nothing bought:
    a file with no version is a file with no field this proxy is missing. The
    warning is what gets the line added before the format does move.
    """
    if raw is None:
        log.warning(
            "%s has no schema_version; reading it as %d.0 — add "
            '`schema_version: "%d.0"`',
            path,
            _CONFIG_SCHEMA_MAJOR,
            _CONFIG_SCHEMA_MAJOR,
        )
        return
    # Quoted in the example, so `1.0` unquoted arrives as a float and `1` as an
    # int. Both are what an operator meant; neither is refused over its type.
    text = str(raw).strip()
    parts = text.split(".")
    # Every part, not only the major: `1.x` has a major this proxy reads and is
    # still not a version, and letting it through would mean honouring a file
    # whose version nobody can compare to the next one. A third part is not
    # refused — `1.0.0` is major 1 by any reading of it, and the major is the
    # whole of the comparison.
    #
    # ASCII digits, not `isdigit()`: that is true of superscripts, which `int`
    # then rejects with a ValueError nobody catches, and of the other scripts'
    # decimal digits, which `int` accepts — so `١.0` would be served as major 1.
    # A version in this file is written in the digits the rest of it is.
    if not all(part.isascii() and part.isdigit() for part in parts):
        raise ConfigError(
            f"{path}: schema_version {text!r} is not a version; this proxy "
            f"reads {_CONFIG_SCHEMA_MAJOR}.x"
        )
    if int(parts[0]) != _CONFIG_SCHEMA_MAJOR:
        raise ConfigError(
            f"{path}: schema_version {text!r} is a format this proxy does not "
            f"read; it reads {_CONFIG_SCHEMA_MAJOR}.x"
        )


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
    # Before anything under `mcp` is read: the version describes the shape this
    # loader is about to assume, so a reader must settle it first or it is
    # parsing on a guess.
    _check_schema_version(path, data.get("schema_version"))
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
            extra = set(entry) - _SERVER_KEYS
            if extra:
                # Said out loud rather than refused. `headers:` is how every
                # mainstream MCP client config spells an upstream credential, so
                # an operator will write it and this proxy will drop it — every
                # call to that upstream 401s, and the cause belongs in the log.
                #
                # A warning and not a `ConfigError`, because the keys that
                # actually appear are inert: `transport: streamable_http` names
                # the only transport this proxy speaks. Refusing to start over
                # a key that changes nothing is a worse outcome for an operator
                # than the silence it was meant to fix.
                log.warning(
                    "%s: upstream '%s' sets %s, which this proxy does not read "
                    "— only `name` and `url`",
                    path,
                    entry["name"],
                    ", ".join(f"`{key}`" for key in sorted(extra)),
                )
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
        try:
            urlsplit(url)
        except ValueError:
            # The scheme check passes an unbalanced IPv6 literal —
            # `http://[::1:8080/mcp` — and `build_gateway` then parses the same
            # string outside any handler, which is the traceback-and-exit-1 the
            # comment above says this loop exists to prevent. The exception is
            # not rendered: `urlsplit` puts the netloc in its message for a
            # value that fails NFKC normalisation, and that netloc is where a
            # url password lives.
            raise ConfigError(
                f"{path}: upstream {entry['name']!r} has a url that cannot be "
                f"parsed: {_clip(url)}"
            ) from None
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
    return seconds_setting("RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS", 30.0)


def _clip(value: object, limit: int = 80) -> str:
    """Render a rejected config entry at a bounded length.

    The file is the operator's, so this bounds a log line rather than an attack
    — a hand-edited YAML with a pasted blob in it should not produce a message
    nobody can read.

    Redacted before it is cut, not after; `TicketHolder.refresh_once` says why.
    """
    text = redact_credentials(repr(value))
    return text if len(text) <= limit else text[:limit] + "…"


# The agent's headers that belong to its own connection to this proxy, not to
# the request: the proxy's client sends its own. `mcp-*` is the agent's MCP
# session with this proxy, `last-event-id` resumes the agent's stream from this
# proxy, and `accept-encoding` is what the agent can decode, while it is this
# proxy's client that decodes the upstream's answer.
_THIS_HOP = frozenset({"accept-encoding", "last-event-id"})


def forwardable(headers: Mapping[str, str]) -> dict[str, str]:
    """The agent's headers that may reach an upstream.

    Every header except the x-rail ones and any with `_` in its name, which no
    interface forwards (`agent_header_may_cross`), and the ones that belong to
    this hop rather than the request. fastmcp has already left out the
    transport's own (`host`, `content-length`, `mcp-session-id`, …).
    """
    return {
        name: value
        for name, value in headers.items()
        if agent_header_may_cross(name)
        and not name.lower().startswith("mcp-")
        and name.lower() not in _THIS_HOP
    }


def upstream_client(**kwargs: Any) -> httpx.AsyncClient:
    """The client every mount dials its upstream with.

    The agent's headers fastmcp hands it are filtered (`forwardable`), so the
    x-rail namespace reaches an upstream only as the injector writes it, with
    the plugin off as much as on.

    Three overrides. Two are about where the ticket may end up — it is on every
    request this client sends, so anything that changes the destination hands it
    to a host the config did not name — and the third puts back what turning the
    second one off would otherwise take away.

    **`follow_redirects=False`.** fastmcp hard-codes it to True and offers no way
    to turn it off, and httpx strips only `Authorization` when it re-sends a
    request to a new origin — so an upstream answering
    `307 Location: https://elsewhere/` receives `x-rail` verbatim, at an address
    chosen by the thing this proxy is standing in front of. A redirect surfaces
    as a failed call instead, which is the right outcome: an MCP endpoint that
    has moved is a configuration change, not something to follow at runtime.

    **`trust_env=False`, and `verify` built with it left on.** `HTTP_PROXY` and
    its neighbours would otherwise route every forwarded call through a host an
    environment variable names; `_exchange` refuses the same thing for the same
    reason, and says it at length. The one flag governs both proxies and CA
    roots, so the context is built separately — otherwise shutting out the first
    would also shut out `SSL_CERT_FILE`.
    """
    if kwargs.get("headers") is not None:
        kwargs = {**kwargs, "headers": forwardable(kwargs["headers"])}
    return httpx.AsyncClient(
        **{
            **kwargs,
            "follow_redirects": False,
            "trust_env": False,
            "verify": httpx.create_ssl_context(trust_env=True),
        }
    )


def build_gateway(holder: TicketHolder | None) -> FastMCP:
    """Mount every configured upstream under one endpoint.

    A mount's name becomes the prefix on every tool it re-exposes, so the
    agent sees `<name>_<tool>`.

    `holder` is None where the plugin is off, and then nothing is attached to
    what goes out — not `x-rail`, and not `x-rail-status` either. That is a
    different state from failing closed, where a proxy that means to identify
    its agent could not: pass-through says nothing about identity at all, and
    writing a status header would claim it had tried.
    """
    gateway = FastMCP(name="datrail-proxy")
    timeout = upstream_timeout()
    injector = XRailInjector(holder) if holder is not None else None
    # Read once, at build time. Per request it would be an environment lookup
    # on the hot path, and a route that could raise a ConfigError into a 500
    # long after startup — `plugin_enabled` raises on a value it does not know.
    enabled = plugin_enabled()

    for srv in load_servers():
        if injector is not None:
            # A credential is refused rather than warned about, because the
            # injector is the client's `auth` and httpx derives Basic auth only
            # when there is none: left alone it is silently dropped and every
            # call to the upstream 401s with nothing naming the cause.
            refuse_a_credential_in_the_url(srv["name"], srv["url"])
            warn_if_in_the_clear(srv["name"], srv["url"])
        transport = StreamableHttpTransport(
            url=srv["url"], auth=injector, httpx_client_factory=upstream_client
        )
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

        # Always namespaced, even with one upstream: two upstreams each with a
        # `search` tool would be indistinguishable without it, and namespacing
        # only once a second entry appeared would rename every tool of the
        # first. So there is no setting for bare tool names.
        gateway.mount(proxy, namespace=srv["name"])
        log.info("mounted '%s' -> %s", srv["name"], srv["url"])

    @gateway.custom_route("/health", methods=["GET"])
    async def health(_request):
        """The process is up, its config parsed, and what it holds by way of
        an identity.

        **200 whether or not a ticket is held**, deliberately. Failing closed is
        a designed state and not a fault: the proxy is serving correctly, and
        restarting it does not make an unreachable Rail Center reachable. A
        health check that killed the process here would turn one outage into a
        crash loop. `ticket` is where the state is, for a reader that wants it.
        """
        return JSONResponse(health_payload(holder, enabled))

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


def build_app(holder: TicketHolder | None) -> ASGIApp:
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
        build_gateway(holder).http_app(transport="streamable-http", stateless_http=True)
    )


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        holder = build_holder()
        app = build_app(holder)
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

    # Awaited, so a wrong sandbox name or a rejected credential shows up while
    # an operator is watching. Never fatal, and the wait is bounded by
    # RAIL_PROXY_TICKET_TIMEOUT_SECONDS — the port is not open until it returns.
    async with running(holder, wait_for_first_fetch=True):
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
                # `proxy_headers` defaults on, and `forwarded_allow_ips` defaults to
                # 127.0.0.1 — which in a sidecar is the sandbox. Left alone, the
                # agent chooses the client address and scheme in this process's own
                # access log by sending `X-Forwarded-For`. Nothing in front of this
                # proxy terminates TLS for it; the sandbox connects to it directly.
                proxy_headers=False,
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
        install_redaction()
        await server.serve()
    return 0
