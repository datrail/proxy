# Security Policy

The DatRail proxy sits in front of an agent's MCP traffic. It is the boundary
that keeps the **`x-rail` ticket** everything downstream trusts out of the
sandbox: it forwards calls to the upstreams its config names, forwards none of
the agent's own headers, and — where a Rail Center is configured — fetches its
own ticket, holds it, and attaches it to everything it forwards.

Two things are worth attacking here. A call can end up attributed to an agent it
did not come from, so the gateway enforces the wrong policy on it and the audit
trail records the wrong agent. Or a ticket can escape.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Email **yusheng@railxia.com** with `SECURITY` in the subject.

Include what an attacker can do (not only what is wrong), the version or commit,
the smallest reproduction you have, and whether you have told anyone else.

## What to expect

| | |
| --- | --- |
| Acknowledgement | within 3 working days |
| First assessment | within 10 working days |
| Progress | at least every 10 working days until it closes |

We ask for **90 days** before public disclosure and will usually be much
faster. You will be credited unless you would rather not be, and if we disagree
that a report is a vulnerability we will say so plainly rather than let it go
quiet.

## Where the sharp edges are

- **The identity boundary.** The proxy must not carry an identity the agent
  gave it. No header the agent supplies is forwarded — every one, not a list of
  names — and anything that gets one past the boundary defeats the whole chain
  and would show up nowhere in the logs as an error. This is the most valuable
  thing to attack and the most valuable thing to report.
- **Ticket handling.** A ticket is a bearer credential for its lifetime. It must
  not reach a log, an error message, a crash dump, or any host other than the
  upstream it was attached for. It is logged as a digest prefix and never as a
  value, a ticket that could not be a header value is refused rather than
  stored, and redirects are not followed — an upstream answering `307` cannot
  name a host to deliver it to.
- **The upstream leg.** The ticket goes out on every forwarded call. Redirects
  are not followed and no ambient proxy setting is read, so nothing but the
  configured address receives it; a plaintext upstream is warned about rather
  than refused, because an http upstream on a private network is an ordinary
  deployment. A ticket reaching a host the config did not name is a report.
- **The control-plane fetch.** It carries a credential out and a ticket back,
  so it refuses to send one over plaintext to anything but loopback, reads no
  ambient proxy setting, caps and refuses to decompress what comes back, and
  bounds the whole exchange — that fetch runs before the listener binds.
  `RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL` turns the first of those off. A
  deployment that sets it is making a choice, not hitting a bug; a way *past*
  the refusal without it is a report.

## Scope

In scope: this repository, its image, anything that causes a request to be
attributed to an agent it did not come from, and anything that puts a live
ticket somewhere it should not be.

Out of scope: vulnerabilities in FastMCP or other dependencies — report those
upstream and we will help; and an agent's own misconfiguration that the proxy
faithfully passes on.
