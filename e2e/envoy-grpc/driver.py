"""Drives requests through Envoy and asserts what reached each server."""

from lib import (
    ANY_XRAIL,
    FORGED,
    FORGED_HEADERS,
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

ENVOY = "http://envoy:15001/mcp"
UPSTREAM = "http://upstream:8080"
OPEN = "http://open:8080"

# Sent with every request besides the forged headers: two more x-rail ones,
# which never cross, and one of the agent's own, which crosses as sent.
AGENT_HEADERS = {
    **FORGED_HEADERS,
    "x-rail-status": "forged-by-the-sandbox",
    "x-rail-foo": "forged-by-the-sandbox",
    "x-trace": "the-agents-own",
}


def run(target, base):
    reset_journal(base)
    drive(ENVOY, {"Host": target, **AGENT_HEADERS}, tool="track_package")


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


print("protected host, registered — the ticket is attached, over HTTPS")
run("upstream.test:8443", UPSTREAM)
expect(
    "the ticket reaches the upstream",
    "some",
    count(equal_to("x-rail", "e2e-opaque-token"), UPSTREAM),
)
expect(
    "no status header alongside it", "none", count(present("x-rail-status"), UPSTREAM)
)
expect("the sandbox's own x-rail never crosses", "none", count(FORGED, UPSTREAM))
expect(
    "every request arrived over HTTPS", "none", count(posts(scheme="http"), UPSTREAM)
)
the_agents_own_headers_cross(UPSTREAM)

print("unprotected host — forwarded as sent, with no x-rail header")
run("open.test:8080", OPEN)
expect("the call is forwarded", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
expect("no x-rail-status crosses", "none", count(present("x-rail-status"), OPEN))
the_agents_own_headers_cross(OPEN)

finish()
