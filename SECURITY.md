# Security Policy

The DatRail proxy sits in front of an agent's MCP traffic and **attaches the
`x-rail` ticket** that everything downstream trusts. Two things can go
wrong. A call can arrive wearing the wrong identity, so the gateway enforces the
wrong policy on it and the audit trail records the wrong agent. Or the ticket
itself can escape, and a ticket is a bearer credential for as long as it lives.

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

- **Ticket attribution.** The proxy decides which agent a call belongs to. A bug
  that attaches one agent's ticket to another agent's request defeats the entire
  chain, and would show up nowhere in the logs as an error. This is the most
  valuable thing to attack and the most valuable thing to report.
- **Ticket handling.** A ticket is a bearer credential for its lifetime. It must
  not reach a log, an error message, a crash dump, or an upstream that did not
  need it.

## Scope

In scope: this repository, its image, the injection path, anything that causes a
request to be attributed to an agent it did not come from, and anything that
puts a live ticket somewhere it should not be.

Out of scope: vulnerabilities in FastMCP or other dependencies — report those
upstream and we will help; and an agent's own misconfiguration that the proxy
faithfully passes on.
