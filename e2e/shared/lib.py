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


def posts(headers=None):
    """A journal pattern: POSTs to /mcp, carrying `headers` if given."""
    pattern = {"method": "POST", "url": "/mcp"}
    if headers:
        pattern["headers"] = headers
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
# Presence, not the forged value: the proxy attaches no `authorization` of its
# own, so any at all upstream came from the caller.
ANY_AUTH = present("authorization")
FORGED = equal_to("x-rail", "forged-by-the-sandbox")

# Every proxy is driven with both, not just the pass-through one. `authorization`
# is driven for a reason the forged `x-rail` cannot cover: no injector writes
# over it. A proxy holding a ticket sets `x-rail` whatever was forwarded, so with
# forwarding back on the forged `x-rail` still never reaches the upstream, and
# only the `authorization` assertion sees the breach.
FORGED_HEADERS = {
    "x-rail": "forged-by-the-sandbox",
    "Authorization": "Bearer forged-by-the-sandbox",
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
def drive(url, headers):
    """An MCP `initialize` and a `tools/call` at `url`, with `headers` added."""
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
                    "name": "delivery_track_package",
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
