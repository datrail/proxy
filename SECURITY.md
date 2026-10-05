# Security Policy

The DatRail proxy sits in front of an agent's MCP traffic. It is the boundary
that keeps the **`x-rail` ticket** everything downstream trusts out of the
sandbox: where a Rail Center is configured, it fetches its own ticket, holds
it, and attaches it to what it forwards to the upstreams its config names.

It comes in two forms:

- **proxy-standalone** is an MCP proxy: it makes every upstream call itself
  and forwards none of the agent's own headers.
- **proxy-envoy-grpc** is a service Envoy calls on each of the agent's HTTP
  requests: Envoy forwards the agent's own request, and the service adds the
  ticket to those for protected hosts.

Two things are worth attacking here. A call can end up attributed to an agent it
did not come from, so the gateway enforces the wrong policy on it and the audit
trail records the wrong agent. Or a ticket can escape.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use the repository's **Security** tab to open a private vulnerability report.

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
  gave it, and anything that gets one past the boundary defeats the whole chain
  and would show up nowhere in the logs as an error. This is the most valuable
  thing to attack and the most valuable thing to report.
  - proxy-standalone forwards no header the agent supplies — every one, not a
    list of names.
  - proxy-envoy-grpc forwards the agent's own headers as sent, by design,
    `Authorization` included: an agent's own credentials for a server are its
    own business. What it must never forward is an `x-rail` or `x-rail-*`
    header the agent wrote: Envoy removes them before calling the service,
    and the service removes any that remain. An agent-written `x-rail` header
    reaching an upstream through Envoy is a report.
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
  bounds the whole exchange — in proxy-standalone that fetch runs before the
  listener binds.
  `RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL` turns the first of those off. A
  deployment that sets it is making a choice, not hitting a bug; a way *past*
  the refusal without it is a report.
- **The Envoy service's socket.** The socket Envoy calls is for Envoy only:
  whatever can connect to it can ask the service what it would attach to a
  request, ticket included. It is created `0660`, for the service's own user
  and group, and the service has no TCP port, since anything on localhost in
  a pod is reachable by the agent. Envoy reaches it by sharing that group; do
  not put the agent in it.
- **Envoy's access log.** The reference Envoy config logs no request header,
  so never `x-rail`. A deployment that adds request headers to its access log
  format must leave `x-rail` out.

## Scope

In scope: this repository, its images, anything that causes a request to be
attributed to an agent it did not come from, and anything that puts a live
ticket somewhere it should not be.

Out of scope: vulnerabilities in FastMCP or other dependencies — report those
upstream and we will help; and an agent's own misconfiguration that the proxy
faithfully passes on.
