# Security Policy

The DatRail proxy sits in front of an agent's MCP traffic. It is the boundary at
which the **`x-rail` ticket** everything downstream trusts is attached — a
mechanism that is **not implemented yet**: today the proxy forwards calls and
attaches nothing, and strips a ticket an agent tries to supply.

Two things will be able to go wrong once it does. A call can arrive wearing the
wrong identity, so the gateway enforces the wrong policy on it and the audit
trail records the wrong agent. Or the ticket itself can escape, and a ticket is
a bearer credential for as long as it lives.

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
  gave it. It strips `x-rail` and `authorization` on the way out for that
  reason; anything that gets one of them past the boundary defeats the whole
  chain and would show up nowhere in the logs as an error. This is the most
  valuable thing to attack and the most valuable thing to report. Ticket
  attribution proper — deciding which agent a call belongs to — arrives with
  the mechanism above.
- **Ticket handling.** A ticket is a bearer credential for its lifetime. It must
  not reach a log, an error message, a crash dump, or an upstream that did not
  need it.

## Scope

In scope: this repository, its image, anything that causes a request to be
attributed to an agent it did not come from, and anything that puts a live
ticket somewhere it should not be.

Out of scope: vulnerabilities in FastMCP or other dependencies — report those
upstream and we will help; and an agent's own misconfiguration that the proxy
faithfully passes on.
