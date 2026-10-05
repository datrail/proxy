"""What every e2e driver asserts with: WireMock's request journal, the header
matchers, `drive` and `expect`.

The assertions are the journal, not log scraping: the question is what crossed
the wire, and the journal is the only thing that answers it.

Standard library only. A driver runs in a stock `python` image with this file
mounted beside it, so nothing outside `e2e/` is needed to run a stack.
"""

import json
import re
import urllib.error
import urllib.request

UPSTREAM = "http://upstream:8080"
ACCEPT = "application/json, text/event-stream"

fails = 0


# Every admin call raises on a non-2xx answer, and that is load-bearing rather
# than tidy. The journal reset moved between WireMock majors — `POST
# /__admin/requests/reset` is gone in 3.x, `DELETE /__admin/requests` replaced
# it — and a 404 there that went unnoticed would leave the previous proxy's
# traffic in the journal, so the next assertion counts headers that another
# container attached and passes for the wrong reason. `urlopen` raises on it,
# which stops the run.
def _admin(method, path, body=None, base=UPSTREAM):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        text = response.read()
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
FORGED_XRAIL = "forged-by-the-sandbox"
FORGED = equal_to("x-rail", FORGED_XRAIL)


def _post(url, payload, headers):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Accept": ACCEPT, "Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(request) as response:
        return response.read().decode()


# The proxy is stateless — it issues no session id — so a call needs no
# handshake state carried between requests. `initialize` is sent anyway
# because that is what a real client does.
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
    # The tool call's body is read rather than discarded, and `"isError":false`
    # is the leg that does the work. fastmcp does not hand the upstream's
    # JSON-RPC error back as an error: it converts it into a *successful* result
    # whose content carries `"isError":true`, so matching `"result"` alone
    # passes on a call that failed outright while every header assertion still
    # counts the POST that carried the failure. `delivery_track_package` is the
    # mount name in the bridge file joined to the tool name the upstream lists,
    # a coupling across three files: break any leg of it and this is what says
    # so. The body is matched as text, not parsed, because the proxy may frame
    # it as an SSE event rather than bare JSON.
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
