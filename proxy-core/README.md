# proxy-core

`proxy.core`, DatRail Proxy's vendor-neutral core: obtaining this proxy's
`x-rail` ticket from Rail Center and putting it on every outbound request. It
imports no other member of this workspace; the standalone host, and any future
interface, builds on it. See the [main README](../README.md) for the whole.

## The ticket

- `TicketSource` fetches a ticket for one host and sandbox. The response shape
  is [`spec/ticket-fetch.schema.json`](../spec/ticket-fetch.schema.json), and
  [`tests/fixtures/tickets.json`](tests/fixtures/tickets.json) is the instance
  the suite validates against it.
- `TicketHolder` keeps a valid one to hand, refreshing it in the background.
- `XRailInjector` puts it on every outbound request; a rotation is picked up
  without a restart.

**The ticket is opaque.** What is inside it is the gateway's contract, and
nothing here looks: expiry comes from `expires_at` alone, and an entry without
one is rejected rather than stored as never expiring.

## Failing closed

With no valid ticket the request still goes out, without `x-rail`, and the
reason in `x-rail-status`:

| `x-rail-status` | Meaning |
|---|---|
| `not-found` | Rail Center says this agent has no ticket |
| `expired` | the ticket lapsed with no replacement |
| `issuer-unreachable` | no current answer from Rail Center |

An expired or absent ticket is never sent, and enforcement belongs to the
gateway, where an absent `x-rail` denies. The reason never goes in `x-rail`
itself.
