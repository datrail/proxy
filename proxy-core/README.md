# proxy-core

`proxy.core`, DatRail Proxy's vendor-neutral core: obtaining this proxy's
`x-rail` ticket from Rail Center and deciding what goes on each outbound
request, judging requests, plus what every interface runs the same way: its
settings, logging and health. It imports no other member of this workspace; every interface,
[proxy-standalone](../proxy-standalone/README.md) and
[proxy-envoy-grpc](../proxy-envoy-grpc/README.md), builds on it. See the
[main README](../README.md) for the whole.

## The ticket

- `TicketSource` fetches a ticket for one host and sandbox. The response shape
  is [`schemas/ticket-fetch.schema.json`](../schemas/ticket-fetch.schema.json),
  and [`tests/fixtures/tickets.json`](tests/fixtures/tickets.json) is the
  instance the suite validates against it.
- `TicketHolder` keeps a valid one to hand, refreshing it in the background.
- `outbound_headers` decides what a request gets: `x-rail`, `x-rail-status`,
  or neither when the plugin is off. `TicketHeaders` adds the log line: a
  warning when the reason there is no ticket changes, not on every request.
- `XRailInjector` puts that on every request an httpx client sends, as
  proxy-standalone's does; proxy-envoy-grpc hands it to Envoy instead. Either
  way a rotation is picked up without a restart.
- `agent_header_may_cross` is the rule every interface applies to the agent's
  own headers: none in the x-rail namespace (`is_xrail_header`), and none with
  `_` in the name, which many servers read as `-`.

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

## Judging requests

Judging lives here, so it never depends on the interface that runs it. Today
proxy-envoy-grpc does; each interface that judges builds the request from what
it received and turns the verdict into its own answer.

- `proxy.core.request`: `Request`, the method, scheme, authority, path and
  headers, as received and never parsed, without any `x-rail` header.
- `proxy.core.judge`: the `Judge` and its verdicts. It answers once the
  headers are in (`Allow`, `Deny` or `NeedsBody`), and, when it needs the body,
  again once the body is read. `AllowAll` is the judge until rules exist. A
  refused request is answered `403` with the text `request refused by policy`.
- `proxy.core.body`: reading a body under bounds, for a judge that needs it.
  `BodyBudget` and `BodyQuota` bound what one body, and every body at once,
  may hold. `build_view` decodes a body by `Content-Encoding` (`gzip`,
  `identity`), never past the cap, and parses it by `Content-Type` (JSON):
  the judge sees that view, never the raw bytes. A body that can't be judged
  as it is gets a `Reason`, which `BodyPolicy` turns into a verdict.

## The process around it

Every interface starts the same way, so these live here rather than in each:

- `proxy.core.settings` reads the variables below and refuses the same
  mistakes with a `ConfigError`, which each interface turns into exit 2. It
  also refuses a credential in an upstream URL, and warns about a plaintext
  one.
- `proxy.core.logs` sets the format and `RAIL_PROXY_LOG_LEVEL`, and puts
  `RedactingFilter` on every handler, so a credential in a URL never reaches a
  log line.
- `proxy.core.lifecycle` builds the holder, runs it (waiting for the first
  fetch, as proxy-standalone does, or not, as proxy-envoy-grpc does), and
  builds the health payload both report.

## Configuration

Every interface reads these, and refuses the same mistakes. Each interface's
README lists its own variables beside them.

#### `RAIL_PLUGIN_ENABLED`

Whether to talk to a Rail Center at all: `true` or `false`, case folded, and
nothing else. Defaults to `false`. Off, the proxy attaches neither header. On,
it attaches `x-rail` when a ticket is held and `x-rail-status` when none could
be got.

It is cross-checked against the Rail Center variables both ways: on with no
Rail Center stops at startup, and so does off beside one that is configured.

#### `RAIL_CENTER_URL`, `RAIL_HOST_ID`, `RAIL_SANDBOX_NAME`

Where the ticket comes from: the Rail Center (e.g.
`https://rail-center.example.com`), the host the proxy runs on, and the agent
sandbox it serves. All three together, or none.

#### `RAIL_AUTH_MODE`

How to authenticate to Rail Center: `none` or `bearer`. Defaults to `none`.

#### `RAIL_AUTH_TOKEN`

The bearer token. Required under `RAIL_AUTH_MODE=bearer`, and ignored
otherwise.

#### `RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL`

Under `RAIL_AUTH_MODE=bearer`, the proxy refuses to start if `RAIL_CENTER_URL`
is plaintext `http://` to anything but loopback, since `RAIL_AUTH_TOKEN` would
be readable on the network. Set this to `true` to send it anyway, for example
to `http://host.docker.internal`. Defaults to `false`.
[SECURITY.md](../SECURITY.md) has more on the risk.

#### `RAIL_PROXY_TICKET_TIMEOUT_SECONDS`

Bounds each fetch from Rail Center. Defaults to `10`.

#### `RAIL_PROXY_REFRESH_SECONDS`

The longest wait between refreshes; a ticket's own expiry is what sets the
cadence. Defaults to `3600`.

#### `RAIL_PROXY_MAX_TICKET_LIFETIME_SECONDS`

Refuse a ticket claiming a longer life than this. Defaults to no bound.

#### `RAIL_PROXY_LOG_LEVEL`

The log level. Defaults to `INFO`.

`RAIL_TICKET_MODE` is retired: set, it stops the proxy at startup, with a
message saying to use `RAIL_PLUGIN_ENABLED`.

### Judging

Read by an interface that judges requests: proxy-envoy-grpc.
[Its README](../proxy-envoy-grpc/README.md#judging-requests) says what Envoy
must send for each.

#### `RAIL_PROXY_ON_JUDGE_FAILURE`

What a request gets when judging it fails, before any verdict: `deny` or
`allow`, case folded. Defaults to `deny`.

#### `RAIL_PROXY_MAX_BODY_BYTES`

The most of one body held, and decoded, in bytes. Defaults to `8388608`
(8 MiB). Past it a body is `too-large`, or `decoded-too-large`.

#### `RAIL_PROXY_BODY_BUDGET_BYTES`

The most of every body held at once, in bytes. Defaults to four times
`RAIL_PROXY_MAX_BODY_BYTES`, and can't be less than it. A body that would pass
it is `over-budget`.

#### `RAIL_PROXY_ALLOW_UNREADABLE_BODY`

The reasons a body that can't be judged is let through unjudged,
comma-separated: any of `not-sent`, `truncated`, `too-large`, `over-budget`,
`unsupported-encoding`, `decoded-too-large`, `malformed`, `timed-out`. Empty
by default: every such body is refused.

#### `RAIL_PROXY_BODY_TIMEOUT_SECONDS`

How long decoding, parsing and judging one body may take before it is
`timed-out`. Defaults to `0.15`, under Envoy's default callout timeouts.
