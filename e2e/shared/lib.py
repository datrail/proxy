"""What every e2e driver asserts with: WireMock's request journal, the header
matchers, `drive` and `expect`. Standard library only, so a stock `python`
image runs it."""

import json
import re
import urllib.error
import urllib.request

UPSTREAM = "http://upstream:8080"

fails = 0


def _request(method, url, body=None, headers=None):
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request) as response:
        return response.read().decode()


def _admin(method, path, body=None, base=UPSTREAM):
    text = _request(method, base + path, body)
    return json.loads(text) if text else None


def count(pattern, base=UPSTREAM):
    """How many requests the upstream received that match `pattern`."""
    return _admin("POST", "/__admin/requests/count", pattern, base)["count"]


def reset_journal(base=UPSTREAM):
    _admin("DELETE", "/__admin/requests", base=base)


def unmatched(base=UPSTREAM):
    """Requests no stub answered.

    Every other assertion counts requests the upstream *matched*, and a stub
    that went missing is invisible to all of them: the client's `GET /mcp` and
    its closing `DELETE /mcp` are answered by stubs nothing else touches, so
    deleting either changes no count at all.
    """
    return len(_admin("GET", "/__admin/requests/unmatched", base=base)["requests"])


def posts(headers=None, scheme=None):
    """A journal pattern: POSTs to /mcp, carrying `headers` and arriving over
    `scheme` (`http` or `https`) if given."""
    pattern = {"method": "POST", "url": "/mcp"}
    if headers:
        pattern["headers"] = headers
    if scheme:
        pattern["scheme"] = scheme
    return pattern


# Presence is `matches: ".*"`. There is no `present` matcher, and `absent:
# false` is not one either — it reads as no constraint at all, so every count
# comes back as the total and every absence assertion passes.
def present(header):
    return posts({header: {"matches": ".*"}})


def equal_to(header, value):
    return posts({header: {"equalTo": value}})


ANY_POST = posts()
ANY_XRAIL = present("x-rail")
ANY_STATUS = present("x-rail-status")
FORGED = equal_to("x-rail", "forged-by-the-sandbox")
FORGED_STATUS = equal_to("x-rail-status", "forged-by-the-sandbox")

# Sent with every request, by every stack: the x-rail namespace forged, two of
# it spelled with `_` for upstreams that read it as `-`, none of which cross,
# and two of the agent's own, which cross as sent. A proxy holding a ticket
# sets `x-rail` whatever was forwarded, so the forged `x-rail` alone can't show
# a breach; the rest of the namespace can.
AGENT_HEADERS = {
    "x-rail": "forged-by-the-sandbox",
    "x-rail-status": "forged-by-the-sandbox",
    "x-rail-foo": "forged-by-the-sandbox",
    "x_rail": "forged-by-the-sandbox",
    "x_rail_status": "forged-by-the-sandbox",
    "x-trace": "the-agents-own",
    "Authorization": "Bearer the-agents-own",
}


def _post(url, payload, headers):
    return _request(
        "POST",
        url,
        payload,
        {"Accept": "application/json, text/event-stream", **headers},
    )


# The proxy is stateless, so `initialize` carries nothing the call needs; it is
# sent because a real client sends it.
def drive(url, headers, tool="delivery_track_package"):
    """An MCP `initialize` and a `tools/call` of `tool` at `url`, with `headers`
    added. The default is the tool as standalone exposes it: the upstream's
    `track_package`, under the name its bridge file mounts it as."""
    _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e-driver", "version": "1"},
            },
        },
        headers,
    )
    # `"isError":false` is the leg that does the work: a proxy may hand an
    # upstream's error back as a *successful* result carrying `"isError":true`,
    # so matching `"result"` alone passes on a call that failed. Matched as
    # text, because the proxy may frame it as an SSE event.
    try:
        body = _post(
            url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {"id": "pkg-1"},
                },
            },
            headers,
        )
    except urllib.error.HTTPError as exc:
        body = f"HTTP {exc.code}"
    if re.search(r'"result".*"isError":\s*false', body, re.DOTALL):
        print("  ok    the tool call returns a result")
    else:
        _fail(f"the tool call returns a result — got {body}")


def expect(what, want, got):
    """`want` is "some" (at least one) or "none" (exactly zero)."""
    if want == "some" and got >= 1:
        print(f"  ok    {what} ({got})")
    elif want == "none" and got == 0:
        print(f"  ok    {what}")
    else:
        _fail(f"{what} — got {got}, wanted {want}")


def check(what, condition):
    """An assertion that is true or false rather than a count."""
    if condition:
        print(f"  ok    {what}")
    else:
        _fail(what)


def the_agents_own_headers_cross(base=UPSTREAM):
    """What `AGENT_HEADERS` must have done at `base`: the agent's own headers
    arrived as sent, and none of the forged x-rail ones did."""
    expect(
        "the agent's own header crosses as sent",
        "some",
        count(equal_to("x-trace", "the-agents-own"), base),
    )
    expect(
        "so does its authorization",
        "some",
        count(equal_to("authorization", "Bearer the-agents-own"), base),
    )
    expect("no forged x-rail-foo crosses", "none", count(present("x-rail-foo"), base))
    expect("nor x_rail", "none", count(present("x_rail"), base))
    expect("nor x_rail_status", "none", count(present("x_rail_status"), base))
    expect("every request matched a stub", "none", unmatched(base))


def _fail(message):
    global fails
    fails += 1
    print(f"  FAIL  {message}")


def finish():
    """Print the verdict and exit with it."""
    print()
    if fails == 0:
        print("e2e: all assertions passed")
        raise SystemExit(0)
    print(f"e2e: {fails} assertion(s) failed")
    raise SystemExit(1)
