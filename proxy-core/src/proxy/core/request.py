"""One request from the agent, as every use case reads it.

It knows nothing of the protocol it arrived by: each interface builds one from
what it received. The method, scheme, authority and path are kept as received
and never parsed here, so building a `Request` can't fail on a target that
`decide` would refuse; a rule that needs one parsed parses it itself.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from proxy.core.xrail_auth import is_xrail_header


@dataclass(frozen=True)
class Request:
    """`headers` are the agent's, in order and with repeats, without any
    x-rail header or pseudo-header: a rule never sees a ticket, and the
    pseudo-headers' values are the fields."""

    method: str
    scheme: str
    authority: str
    path: str
    headers: tuple[tuple[str, str], ...]


def build_request(
    method: str,
    scheme: str,
    authority: str,
    path: str,
    headers: Iterable[tuple[str, str]],
) -> Request:
    """A `Request` from what an interface received. `path` includes the
    query, as in the request line."""
    return Request(
        method=method,
        scheme=scheme,
        authority=authority,
        path=path,
        headers=tuple(
            (name, value)
            for name, value in headers
            if not name.startswith(":") and not is_xrail_header(name)
        ),
    )
