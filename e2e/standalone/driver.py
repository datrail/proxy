"""Drives each standalone proxy and asserts what reached the upstream."""

from lib import (
    ANY_AUTH,
    ANY_POST,
    ANY_STATUS,
    ANY_XRAIL,
    FORGED,
    FORGED_HEADERS,
    count,
    drive,
    equal_to,
    expect,
    finish,
    reset_journal,
    unmatched,
)

WITH_TICKET = equal_to("x-rail", "e2e-opaque-token")


def run(host):
    reset_journal()
    drive(f"http://{host}:8091/mcp", FORGED_HEADERS)


def the_boundary_holds():
    expect("the sandbox's own x-rail never crosses", "none", count(FORGED))
    expect("no authorization crosses", "none", count(ANY_AUTH))
    expect("every request matched a stub", "none", unmatched())


def fails_closed_with(reason):
    expect("the call is still forwarded", "some", count(ANY_POST))
    expect("no identity is attached", "none", count(ANY_XRAIL))
    expect(
        f"and it says why: {reason}",
        "some",
        count(equal_to("x-rail-status", reason)),
    )
    the_boundary_holds()


print("registered proxy — holds a ticket and attaches it")
run("proxy")
expect("the ticket reaches the upstream", "some", count(WITH_TICKET))
expect("no status header alongside it", "none", count(ANY_STATUS))
the_boundary_holds()

print("unregistered proxy — Rail Center holds no ticket for it")
run("proxy-unregistered")
fails_closed_with("not-found")

print("expired proxy — the only ticket Rail Center has for it has lapsed")
run("proxy-expired")
fails_closed_with("expired")

print("issuer-down proxy — Rail Center answers it with a 500")
run("proxy-issuer-down")
fails_closed_with("issuer-unreachable")

print("pass-through proxy — no control plane configured")
run("proxy-passthrough")
expect("the call is still forwarded", "some", count(ANY_POST))
expect("neither header is attached", "none", count(ANY_XRAIL))
expect("not even a status header", "none", count(ANY_STATUS))
the_boundary_holds()

finish()
