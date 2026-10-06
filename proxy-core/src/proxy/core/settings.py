"""Where this proxy's ticket comes from, read from the environment.

Every interface reads the same variables and refuses the same mistakes, so the
settings for the ticket half live here rather than in any one of them: the
plugin flag, the Rail Center it fetches from, how it authenticates, and the
bounds on the fetch. What an interface serves, and how it is bound, stays in
that interface.
"""

import logging
import math
import os
from urllib.parse import urlsplit

from proxy.core.xrail_auth import TicketSource, is_loopback

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """The configuration cannot be served. Raised rather than exited: this
    module is importable, and a library that calls `sys.exit` takes its caller
    down past every `except Exception` between them. `main()` is where this
    becomes the container's exit code."""


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
    refuses a Rail Center configured beside an off flag, and by the advice that
    tells an operator what a plain proxy has to shed — in `build_ticket_source`
    when the Rail Center is incomplete, and in `refuse_a_credential_in_the_url`.
    Advice derived from a second list stops being true the moment either grows.
    """
    _, named, _ = _naming_variables()
    return named + _naming_credentials()


def max_ticket_lifetime() -> float | None:
    """An operator's ceiling on a ticket's lifetime. Unset by default — see
    `xrail_auth.IMPLAUSIBLE_TICKET_LIFETIME_SEC` for why there is no built-in
    one."""
    return seconds_setting("RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS", None)


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
    return seconds_setting("RAIL_PROXY_REFRESH_SECONDS", 3600.0)


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


def seconds_setting(name: str, default: float | None) -> float | None:
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


def ticket_timeout() -> float:
    """Seconds to wait on Rail Center for this proxy's own ticket.

    Its own setting rather than the upstream one, because the wait falls in a
    different place: where an interface starts its holder with
    `wait_for_first_fetch` (standalone does), the startup fetch runs before the
    listener is bound, so this value is how long a Rail Center that does not
    answer delays the port and `/health`. Sharing the upstream's value would
    mean raising that for a slow tool server also lengthened a container's time
    to first health check, which is how a startup delay becomes a crash loop.
    """
    return seconds_setting("RAIL_PROXY_TICKET_TIMEOUT_SECONDS", 10.0)


def _in_the_clear(url: str) -> bool:
    """True where a request to `url` crosses a network unencrypted."""
    parts = urlsplit(url)
    return parts.scheme != "https" and not is_loopback(parts.hostname)


def refuse_a_credential_in_the_url(name: str, url: str) -> None:
    """Refuse an upstream `url` that carries a credential of its own.

    Called only while a ticket is being attached: an upstream reached with a
    ticket is reached with that and nothing else.

    `username or password`, matching httpx: it derives Basic auth from either,
    so `https://token@host/` is as much a credential as `https://u:p@host/`.
    Reading only the password lets the one-part form past — which is the same
    mistake `TicketSource` documents on the fetch leg.

    The advice names the whole action rather than the flag alone, and names it
    out of `_naming_a_rail_center()` rather than a list of its own: a ticket is
    attached only where the flag is on *and* a Rail Center is configured, so
    advice short of what the cross-check keys on lands an operator on
    `build_ticket_source`'s contradictory-intent refusal instead.
    """
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ConfigError(
            f"upstream '{name}' carries a credential in its url, "
            "which cannot be sent while an x-rail ticket is being attached "
            "— remove it, or unset RAIL_PLUGIN_ENABLED and "
            + ", ".join(_naming_a_rail_center())
            + " to forward without a ticket"
        )


def warn_if_in_the_clear(name: str, url: str) -> None:
    """Say so where the ticket will cross a network unencrypted to `url`.

    Called only while a ticket is being attached. Not a refusal: an http
    upstream on a private network is an ordinary deployment, and the packaged
    example is one. But the ticket goes out on every forwarded call, so an
    operator should know it is readable — the same fact `TicketSource` refuses
    over for a credential it is *sending*.
    """
    if _in_the_clear(url):
        log.warning(
            "'%s' is plaintext http — the x-rail ticket is readable by "
            "anyone on the path to %s",
            name,
            url,
        )
