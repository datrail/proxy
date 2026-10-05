"""Drives requests through each Envoy and asserts what reached each server."""

import json
import ssl
import urllib.error
import urllib.request

from lib import (
    ANY_XRAIL,
    FORGED,
    FORGED_HEADERS,
    check,
    count,
    drive,
    equal_to,
    expect,
    finish,
    posts,
    present,
    reset_journal,
    unmatched,
)

# The servers, by their journals' admin address.
UPSTREAM = "http://upstream:8080"
AGENT_PORT = "http://upstream-agent-port:8080"
UPSTREAM_443 = "http://upstream-443:8080"
PLAIN = "http://upstream-plain:8080"
OPEN = "http://open:8080"
EVIL = "http://evil:8080"

# Sent with every request besides the forged headers: two more x-rail ones,
# which never cross, and one of the agent's own, which crosses as sent.
AGENT_HEADERS = {
    **FORGED_HEADERS,
    "x-rail-status": "forged-by-the-sandbox",
    "x-rail-foo": "forged-by-the-sandbox",
    "x-trace": "the-agents-own",
}


def envoy(name):
    return f"http://{name}:15001/mcp"


def run(via, target, base):
    """MCP through Envoy `via` to `target`, with the journal of `base` reset."""
    reset_journal(base)
    drive(envoy(via), {"Host": target, **AGENT_HEADERS}, tool="track_package")


def send(via, target, path="/mcp", method="POST"):
    """One request through Envoy `via`, not following redirects. Returns the
    status and body, whatever the status."""
    request = urllib.request.Request(
        f"http://{via}:15001{path}",
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"Host": target, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def the_agents_own_headers_cross(base):
    expect(
        "the agent's own header crosses as sent",
        "some",
        count(equal_to("x-trace", "the-agents-own"), base),
    )
    expect(
        "so does its authorization",
        "some",
        count(equal_to("authorization", "Bearer forged-by-the-sandbox"), base),
    )
    expect("no forged x-rail-foo crosses", "none", count(present("x-rail-foo"), base))
    expect("every request matched a stub", "none", unmatched(base))


def ticketed(base, scheme="https"):
    expect(
        "the ticket arrives",
        "some",
        count(equal_to("x-rail", "e2e-opaque-token"), base),
    )
    expect(
        f"every request arrived over {scheme}",
        "some",
        count(posts(scheme=scheme), base),
    )
    other = "http" if scheme == "https" else "https"
    expect(f"none over {other}", "none", count(posts(scheme=other), base))
    expect("every request matched a stub", "none", unmatched(base))


print("protected host, registered — the ticket is attached, over HTTPS")
run("envoy", "upstream.test:8443", UPSTREAM)
ticketed(UPSTREAM)
expect(
    "no status header alongside it", "none", count(present("x-rail-status"), UPSTREAM)
)
expect("the sandbox's own x-rail never crosses", "none", count(FORGED, UPSTREAM))
the_agents_own_headers_cross(UPSTREAM)

for via, reason in [
    ("envoy-unregistered", "not-found"),
    ("envoy-expired", "expired"),
    ("envoy-issuer-down", "issuer-unreachable"),
]:
    print(f"protected host, {reason} — no identity, and the reason, over HTTPS")
    run(via, "upstream.test:8443", UPSTREAM)
    expect(
        "the call is forwarded over HTTPS",
        "some",
        count(posts(scheme="https"), UPSTREAM),
    )
    expect("no identity is attached", "none", count(ANY_XRAIL, UPSTREAM))
    expect(
        f"and it says why: {reason}",
        "some",
        count(equal_to("x-rail-status", reason), UPSTREAM),
    )
    expect(
        "the sandbox's own x-rail-status never crosses",
        "none",
        count(equal_to("x-rail-status", "forged-by-the-sandbox"), UPSTREAM),
    )
    the_agents_own_headers_cross(UPSTREAM)

print("protected host, plugin off — still over HTTPS, with neither header")
run("envoy-passthrough", "upstream.test:8443", UPSTREAM)
expect(
    "the call is forwarded over HTTPS", "some", count(posts(scheme="https"), UPSTREAM)
)
expect("neither header is attached", "none", count(ANY_XRAIL, UPSTREAM))
expect("not even a status header", "none", count(present("x-rail-status"), UPSTREAM))
the_agents_own_headers_cross(UPSTREAM)

print("unprotected host — forwarded as sent, with no x-rail header")
run("envoy", "open.test:8080", OPEN)
expect("the call is forwarded over HTTP", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
expect("no x-rail-status crosses", "none", count(present("x-rail-status"), OPEN))
the_agents_own_headers_cross(OPEN)

for target in ["upstream.test", "upstream.test:80", "upstream.test:9999"]:
    print(f"{target} — reached at its configured port, 8443")
    run("envoy", target, UPSTREAM)
    ticketed(UPSTREAM)

print("upstream-agent-port.test:9443 — a bare entry keeps the agent's port")
run("envoy", "upstream-agent-port.test:9443", AGENT_PORT)
ticketed(AGENT_PORT)

for target in ["upstream-443.test", "upstream-443.test:80"]:
    print(f"{target} — a bare entry with no port goes to HTTPS's 443")
    run("envoy", target, UPSTREAM_443)
    ticketed(UPSTREAM_443)

print("upstream-plain.test:8080 — an http:// entry is not upgraded")
run("envoy", "upstream-plain.test:8080", PLAIN)
ticketed(PLAIN, scheme="http")

print("upstream.test misresolved to evil — the certificate check refuses it")
# First, that evil serves TLS on 8443 at all, so a closed port can't pass for a
# refused certificate.
context = ssl.create_default_context(cafile="/certs/ca.crt")
with urllib.request.urlopen(
    "https://evil.test:8443/__admin/health", context=context
) as response:
    check("evil.test answers over HTTPS on 8443", response.status == 200)
# Then the request: Envoy connects to evil, which answers TLS, but evil sees no
# request, so the TLS handshake is what failed. Envoy's access log names the
# cause (CERTIFICATE_VERIFY_FAILED, expected upstream.test, got evil.test); its
# 503 body doesn't, and the driver can't read another container's log.
reset_journal(EVIL)
status, _ = send("envoy-misresolved", "upstream.test:8443")
check("the request fails with a 503", status == 503)
expect("evil sees no request", "none", count(posts(), EVIL))

print("no extension — forwarded with the x-rail headers removed, and no status")
reset_journal(OPEN)
drive(
    envoy("envoy-no-ext"),
    {"Host": "open.test:8080", **AGENT_HEADERS},
    tool="track_package",
)
expect("the call is forwarded", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
expect("no x-rail-status crosses", "none", count(present("x-rail-status"), OPEN))
the_agents_own_headers_cross(OPEN)
reset_journal(UPSTREAM)
status, _ = send("envoy-no-ext", "upstream.test")
check("a protected host is not routed: plain HTTP to port 80 fails", status == 503)
expect("upstream sees no request", "none", count(posts(), UPSTREAM))

print("a redirect from a protected host is not followed")
stub = {
    "request": {"method": "GET", "urlPath": "/redirect"},
    "response": {
        "status": 302,
        "headers": {"Location": "http://open.test:8080/followed"},
    },
}
urllib.request.urlopen(
    urllib.request.Request(
        f"{UPSTREAM}/__admin/mappings",
        data=json.dumps(stub).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
).read()
reset_journal(OPEN)
status, _ = send("envoy", "upstream.test:8443", path="/redirect", method="GET")
check("the agent gets the 302", status == 302)
expect("Envoy did not follow it", "none", count({"urlPath": "/followed"}, OPEN))

finish()
