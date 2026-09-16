"""Receive MCP calls from an agent and forward them to its configured upstreams.

The proxy is a sidecar: one process serves one agent, mounting the upstreams
named in its config file and re-exposing their tools under a namespace.

What this module holds is the path a request travels, the configuration behind
it, and the wiring that puts this proxy's own `x-rail` ticket on everything it
forwards. Obtaining and holding that ticket is `xrail_auth`'s.

It is also the boundary: no header the agent supplies reaches an upstream.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import traceback
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

from fastmcp_proxy.xrail_auth import (
    TicketHolder,
    TicketSource,
    XRailInjector,
    is_loopback,
    redact_credentials,
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


#: How this proxy authenticates to Rail Center. `gcp` is a value the platform
#: defines and this component does not implement, so it is refused rather than
#: quietly treated as `none` — which would 401 on every fetch with nothing
#: saying why.
_AUTH_MODES = {"none", "bearer"}


#: The three that name where a ticket comes from. All of them, or none.
_TICKET_SETTINGS = ("RAIL_CENTER_URL", "RAIL_HOST_ID", "RAIL_SANDBOX_NAME")


def _naming_variables() -> tuple[dict[str, str], list[str], list[str]]:
    """The three, split into what carries a value and what does not.

    Every message about them names the variables it is talking about. An
    operator reading a container log has no way to see which of three they got
    wrong, and "a Rail Center is misconfigured" sends them to check all of it.
    """
    values = {name: os.environ.get(name, "").strip() for name in _TICKET_SETTINGS}
    return (
        values,
        [name for name, value in values.items() if value],
        [name for name, value in values.items() if not value],
    )


def ticket_settings() -> dict[str, str] | None:
    """Where this proxy's ticket comes from, or None if it has no control plane.

    All three together, or none. `TicketSource` says why the sandbox name is not
    optional.
    """
    values, named, missing = _naming_variables()
    if not named:
        return None
    if missing:
        raise ConfigError(
            "a Rail Center is partly configured; also required: " + ", ".join(missing)
        )
    return {
        "url": values["RAIL_CENTER_URL"],
        "host_id": values["RAIL_HOST_ID"],
        "sandbox_name": values["RAIL_SANDBOX_NAME"],
    }


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


def _naming_credentials() -> list[str]:
    """Which auth variables carry something only an enrolled proxy has a use for.

    By value and not by presence, because declaring the whole `RAIL_*` block and
    filling in the half a deployment needs is the ordinary shape: the packaged
    `.env.example` ships both keys empty, and a compose file passing
    `RAIL_AUTH_MODE=${RAIL_AUTH_MODE:-none}` beside an empty token is a proxy
    that authenticates with nothing. Neither names an intent to attach. A token
    with a value, or a mode that is not `none`, is configuration that exists for
    the ticket fetch and for nothing else in this component.
    """
    mode = os.environ.get("RAIL_AUTH_MODE", "").strip().lower()
    token = os.environ.get("RAIL_AUTH_TOKEN", "").strip()
    carried = (
        ("RAIL_AUTH_MODE", bool(mode) and mode != "none"),
        ("RAIL_AUTH_TOKEN", bool(token)),
    )
    return [name for name, carries in carried if carries]


def _naming_a_rail_center() -> list[str]:
    """Every variable set on this proxy that names an intent to attach.

    One list, read both by the cross-check in `build_ticket_source` that
    refuses a Rail Center configured beside an off flag, and by the advice in
    `build_gateway` that tells an operator what a plain proxy has to shed.
    Advice derived from a second list stops being true the moment either grows.
    """
    _, named, _ = _naming_variables()
    return named + _naming_credentials()


def max_ticket_lifetime() -> float | None:
    """An operator's ceiling on a ticket's lifetime. Unset by default — see
    `xrail_auth.IMPLAUSIBLE_TICKET_LIFETIME_SEC` for why there is no built-in
    one."""
    return _seconds("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", None)


def allow_insecure_credential() -> bool:
    """Whether to send a credential to Rail Center over plaintext http.

    Off by default. Loopback and https need no override; this is for a
    plaintext, non-loopback issuer — a container reaching
    `http://host.docker.internal:…`, say, which `is_loopback` deliberately does
    not exempt.
    """
    raw = os.environ.get("RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL", "").strip().lower()
    return raw in ("1", "true", "yes")


#: Platform-wide rather than `RAIL_PROXY_*`: whether RailXia is installed is one
#: fact about a zone, and the prefix claims only knobs this component owns alone.
#:
#: It replaces `RAIL_TICKET_MODE`, which named a posture it had stopped
#: carrying: a gateway takes its posture from the policy bundle, and a proxy has
#: never had one — `observe` and `enforce` were identical here, both attaching.
#: What was left was the boolean this is.
_PLUGIN_ENABLED_VALUES = {"true": True, "false": False}


def plugin_enabled() -> bool:
    """Whether this proxy talks to a Rail Center at all.

    Off by default, so a plain proxy that nobody has given RailXia
    configuration needs no variable and comes up forwarding. The danger of that
    default — a dropped variable silently unenrolling a proxy that was
    attaching an hour ago — is not carried by the default but by
    `build_ticket_source`, which refuses to start where a Rail Center is
    configured and the flag is off. What that covers is the variable dropped on
    its own: a deployment that loses the whole `RAIL_*` block at once — an env
    file that fails to mount — leaves nothing naming an intent to attach, and
    comes up forwarding.

    Parsed strictly, folding case and taking nothing but `true` or `false`: a
    proxy that read `ture` as off would unenroll on a typo, which is the silent
    failure this variable exists to end.
    """
    _refuse_retired_ticket_mode()
    raw = os.environ.get("RAIL_PLUGIN_ENABLED", "").strip().lower()
    if not raw:
        return False
    if raw not in _PLUGIN_ENABLED_VALUES:
        raise ConfigError(f"RAIL_PLUGIN_ENABLED={raw!r} is not true or false")
    return _PLUGIN_ENABLED_VALUES[raw]


def _refuse_retired_ticket_mode() -> None:
    """Stop on a `RAIL_TICKET_MODE` nothing reads any more.

    Ignoring it is the failure the whole change exists to end: an operator who
    sets a variable believing it configures a posture, and gets whatever the
    default happens to be. Named in the refusal rather than merely rejected, so
    the message is the migration.
    """
    if os.environ.get("RAIL_TICKET_MODE", "").strip():
        raise ConfigError(
            "RAIL_TICKET_MODE is no longer read. Whether this proxy talks to a "
            "Rail Center is RAIL_PLUGIN_ENABLED=true|false; the posture it used "
            "to name is a gateway's, and arrives in the policy bundle."
        )


def refresh_seconds() -> float:
    """Upper bound between refreshes.

    Only an upper bound: a ticket's own expiry drives the real cadence, so this
    caps a long-lived one rather than setting the interval.
    """
    return _seconds("RAIL_PROXY_REFRESH_SECONDS", 3600.0)


def build_ticket_source() -> TicketSource | None:
    """The source this proxy fetches its own ticket from, or None.

    The flag and the issuer are cross-checked both ways. A proxy that means to
    attach with nothing to fetch from would otherwise come up healthy and
    forward every call unstamped; a Rail Center configured beside a plugin that
    is off is contradictory intent, and guessing which half was meant is not
    this component's call.

    That second check is what makes `RAIL_PLUGIN_ENABLED` safe to default off.
    A deployment that loses the flag is a deployment that still names a Rail
    Center — by its address or by the credential it authenticates to it with —
    so it stops here instead of quietly becoming a plain proxy.
    """
    enabled = plugin_enabled()
    _, _, missing = _naming_variables()

    # The flag is read before the settings are, so an off plugin beside a
    # *partly* configured Rail Center reports the contradiction rather than the
    # incompleteness — an operator who set one variable by accident is not
    # being asked to finish the job.
    if not enabled:
        # The credential names a Rail Center as much as the address does. The
        # two ordinarily arrive from different places — a token from a Secret,
        # the address from a ConfigMap — so a deployment can lose the three and
        # keep the one variable nothing but an enrolled proxy has a use for,
        # which is a loss the three on their own do not see.
        naming = _naming_a_rail_center()
        if naming:
            raise ConfigError(
                "RAIL_PLUGIN_ENABLED is off, so nothing is attached, but "
                + ", ".join(naming)
                + " is set — one of the two was not meant. Set "
                "RAIL_PLUGIN_ENABLED=true to attach, or unset them to forward "
                "without a ticket."
            )
        return None
    if missing:
        # The advice names the whole action rather than the flag alone, and
        # names it out of `_naming_a_rail_center()` rather than a list of its
        # own: a proxy that cannot find its Rail Center may still name one by
        # the part it does hold — an address, or the credential it would fetch
        # with — and unsetting the flag alone lands it on the contradictory-
        # intent refusal above. Where nothing else names one there is nothing
        # to add, which is the shape the advice was written for.
        also_naming = _naming_a_rail_center()
        raise ConfigError(
            "RAIL_PLUGIN_ENABLED=true attaches an identity, so a Rail Center "
            "to fetch one from is required; not set: "
            + ", ".join(missing)
            + ". To forward without one, unset RAIL_PLUGIN_ENABLED"
            + (" and " + ", ".join(also_naming) if also_naming else "")
            + "."
        )
    # `missing` is empty, so this returns the dict rather than None.
    settings = ticket_settings()
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

    Redacted before it is cut, not after; `TicketHolder.refresh_once` says why.
    """
    text = redact_credentials(repr(value))
    return text if len(text) <= limit else text[:limit] + "…"


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


def _in_the_clear(url: str) -> bool:
    """True where a request to `url` crosses a network unencrypted."""
    parts = urlsplit(url)
    return parts.scheme != "https" and not is_loopback(parts.hostname)


def upstream_client(**kwargs: Any) -> httpx.AsyncClient:
    """The client every mount dials its upstream with.

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
        parts = urlsplit(srv["url"])
        if injector is not None and (parts.username or parts.password):
            # `username or password`, matching httpx: it derives Basic auth from
            # either, so `https://token@host/` is as much a credential as
            # `https://u:p@host/`. Reading only the password lets the one-part
            # form past — which is the same mistake `TicketSource` documents on
            # the fetch leg.
            #
            # Refused rather than warned, because the injector is the client's
            # `auth` and httpx derives Basic auth only when there is none: left
            # alone this credential is silently dropped and every call to the
            # upstream 401s with nothing naming the cause.
            #
            # The advice names the whole action rather than the flag alone,
            # and names it out of `_naming_a_rail_center()` rather than a list
            # of its own: an injector exists only where the flag is on *and* a
            # Rail Center is configured, so advice short of what the
            # cross-check keys on lands an operator on
            # `build_ticket_source`'s contradictory-intent refusal instead.
            raise ConfigError(
                f"upstream '{srv['name']}' carries a credential in its url, "
                "which cannot be sent while an x-rail ticket is being attached "
                "— remove it, or unset RAIL_PLUGIN_ENABLED and "
                + ", ".join(_naming_a_rail_center())
                + " to forward without a ticket"
            )
        if injector is not None and _in_the_clear(srv["url"]):
            # Not a refusal: an http upstream on a private network is an
            # ordinary deployment, and the packaged example is one. But the
            # ticket goes out on every forwarded call, so an operator should
            # know it is readable — the same fact `TicketSource` refuses over
            # for a credential it is *sending*.
            log.warning(
                "'%s' is plaintext http — the x-rail ticket is readable by "
                "anyone on the path to %s",
                srv["name"],
                srv["url"],
            )
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
        return JSONResponse(
            {
                "status": "ok",
                "plugin_enabled": enabled,
                "ticket": holder.status if holder is not None else None,
            }
        )

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


async def main() -> int:
    import uvicorn

    configure_logging()
    try:
        source = build_ticket_source()
        holder = (
            TicketHolder(source, refresh_seconds=refresh_seconds())
            if source is not None
            else None
        )
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

    if holder is None:
        log.info(
            "RAIL_PLUGIN_ENABLED is off — nothing is attached to what is forwarded"
        )
    else:
        # Awaited, so a wrong sandbox name or a rejected credential shows up
        # while an operator is watching. Never fatal, and the wait is bounded by
        # RAIL_PROXY_TICKET_TIMEOUT_SECONDS — the port is not open until it
        # returns.
        await holder.start()

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
    _install_redaction()
    try:
        await server.serve()
    finally:
        # The refresh loop outlives `serve()` otherwise, and asyncio.run then
        # cancels it during interpreter shutdown — which surfaces as a
        # traceback on a clean SIGTERM.
        if holder is not None:
            await holder.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
