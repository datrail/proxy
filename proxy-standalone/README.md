# proxy-standalone

`proxy.standalone`, the FastMCP host that runs
[proxy-core](../proxy-core/README.md) between an agent and its MCP servers:
process configuration, the bridge file, and the HTTP server. Its Dockerfile
builds `ghcr.io/datrail/proxy`. See the [main README](../README.md) for the
whole.

## Architecture

```mermaid
flowchart LR
  agent["Agent (MCP client)"] -->|"MCP, POST /mcp"| host
  subgraph standalone["proxy-standalone"]
    host["FastMCP host: every upstream's tools, as name_tool"] --> client["an MCP client per upstream"]
    holder["TicketHolder"] -->|"x-rail or x-rail-status"| client
  end
  bridge["bridge.yaml"] -.->|"the upstreams"| host
  center["Rail Center"] -->|"ticket for host plus sandbox"| holder
  client -->|"MCP plus x-rail"| upstream["upstream: DatRail Gateway or an MCP server"]
```

The agent speaks MCP to the proxy, and the proxy's own MCP clients make every
call upstream, carrying the agent's headers as
[proxy-envoy-grpc](../proxy-envoy-grpc/README.md) does:

- **The agent's own headers** cross on every upstream request its call
  causes, `Authorization` included. None in the x-rail namespace (`x-rail`,
  `x-rail-*`) ever does, nor any with `_` in its name, nor those about the
  agent's own connection to the proxy (`mcp-*`, `last-event-id`,
  `accept-encoding`). The proxy adds `x-rail` with the ticket, or
  `x-rail-status` with the reason there is none, or neither when the plugin is
  off.
- **Only the bridge file's upstreams** get the ticket. Redirects are not
  followed, and no ambient proxy setting is read.
- **Startup waits for the first fetch**, bounded by
  `RAIL_PROXY_TICKET_TIMEOUT_SECONDS`, before the port is bound. A failed fetch
  doesn't stop it: calls then carry `x-rail-status`.

### Where it still differs from the Envoy form

Because it makes the upstream calls itself:

- **One agent call can be several upstream requests**, each with the agent's
  headers: an MCP handshake (`initialize`, `notifications/initialized`), a
  `GET` stream, the call, and a `DELETE` closing the session.
- **The transport's headers are the proxy client's own**: `host`, `accept`,
  `content-type`, `content-length`, `mcp-session-id`, `mcp-protocol-version`,
  `connection` and `accept-encoding`.
- **A header the agent repeats** reaches the upstream once, with its last
  value.
- **The upstream's response headers** don't reach the agent: the proxy
  answers in its own MCP session.
- **No `X-Forwarded-Proto`**, which Envoy adds on its own.

## Run

The main README's [quick start](../README.md#quick-start) runs the image
forwarding-only, and that is the whole of it: `RAIL_PLUGIN_ENABLED` is off by
default, so a proxy nobody has given RailXia configuration needs no variable at
all. The image has no bridge file of its own: without one mounted it stops at
startup.

To attach an identity, set `RAIL_PLUGIN_ENABLED=true` and add
`RAIL_CENTER_URL`, `RAIL_HOST_ID`, and `RAIL_SANDBOX_NAME` — those three beside
a plugin that is off is refused at startup, so a deployment cannot lose the flag
by itself and quietly stop attaching. Losing the whole block at once still can:
an env file that fails to mount takes the flag and the three with it, and the
proxy comes up as a plain proxy, saying so at INFO. Watch that the variables
arrive, not just that the container is healthy.

## Configuration

It reads the variables every interface shares: see
[proxy-core's configuration](../proxy-core/README.md#configuration).
[`.env.example`](../.env.example) lists every variable.

#### `RAIL_PROXY_CONFIG_FILE`

The bridge file: the upstream MCP servers, the one thing an environment
variable can't express. Each upstream's tools are re-exposed as
`<name>_<tool>`. Start from [`bridge.yaml.example`](bridge.yaml.example).

Required: the proxy stops at startup without one. Defaults to
`standalone/bridge.yaml`, relative to the working directory. The image sets
it to `/app/standalone/bridge.yaml` and ships no file there, so mount one.

#### `RAIL_PROXY_BIND`

The interface to listen on. Defaults to `0.0.0.0`.

#### `RAIL_PROXY_PORT`

The port to listen on. Defaults to `8091`.

#### `RAIL_PROXY_UPSTREAM_TIMEOUT_SECONDS`

How long a call waits on an upstream before the agent is told it timed out.
Defaults to `30`.

## Endpoints

- `POST /mcp`: the proxied MCP endpoint.
- `GET /health`: liveness, `200` whether or not a ticket is held, with whether
  the plugin is on and a fingerprint and expiry of what is held — never the
  ticket.

## From source

```bash
make init
cp proxy-standalone/bridge.yaml.example bridge.yaml   # then edit it
RAIL_PROXY_CONFIG_FILE=bridge.yaml uv run python -m proxy.standalone
```
