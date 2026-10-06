# proxy-envoy-grpc

`proxy.envoy_grpc`, an Envoy gRPC extension service. Envoy calls it on each
request, and it adds the `x-rail` headers to requests for protected hosts. See
the [main README](../README.md) for the whole.

## How it works

The agent's HTTP traffic goes through Envoy, and Envoy calls this service
(ext_authz, on a unix socket) for every request before forwarding it. Envoy
forwards the agent's own request, rather than building a new one as
[proxy-standalone](../proxy-standalone/README.md) does.

Before the call, Envoy removes any `x-rail` or `x-rail-*` header the agent
sent. The service then looks at the request's host:

- **Not protected:** it changes nothing, and Envoy forwards the request as the
  agent sent it.
- **Protected:** it sets `x-rail` with the ticket, or `x-rail-status` with the
  reason there is none (`not-found`, `expired` or `issuer-unreachable`), or
  neither when the plugin is off. It also tells Envoy how to reach the host:
  over HTTPS unless its entry says `http://`, at the entry's port if it names
  one, else the agent's.

The agent's other headers are forwarded as sent, to protected hosts too,
`Authorization` included, except any with `_` in its name: Envoy drops those,
since many servers read `_` as `-`, and `x_rail` would reach one as `x-rail`.

The service always answers OK: it changes a request, and never refuses one.

### Principles

1. **Envoy fails open.** If the service can't be reached, Envoy still
   forwards the request.
2. **The service fails open.** No ticket yet, Rail Center down, or an error
   inside the service: the request is still forwarded.
3. **Only the service adds `x-rail-status`.** Envoy removes the agent's, and
   never adds one of its own.
4. **HTTPS only for protected hosts, unless their entry says `http://`.** An
   unprotected request is never upgraded.
5. **The agent's port, unless the entry names one.**

### When the service is down

Envoy forwards every request as if its host weren't protected:

- no `x-rail` and no `x-rail-status`, so a gateway sees no identity and no
  reason, as from an agent with no proxy at all;
- no routing: a protected host is reached over plain HTTP at the host and port
  the agent sent, so the request usually fails to connect;
- the ticket is never sent, since only the service attaches it.

Envoy doesn't report this in its log: the access log line for a request
forwarded this way carries no flag. Only Envoy's statistics count it
(`ext_authz.failure_mode_allowed`), and the reference config exposes none, so
watch the service's own health probe instead.

### At startup

The socket is bound at once, without waiting for the first ticket. Until it
arrives, protected requests get `x-rail-status: issuer-unreachable`.

## Run it beside Envoy

- **The image** is `ghcr.io/datrail/proxy-envoy-grpc`, released with the same
  versions as `ghcr.io/datrail/proxy`. It is built from
  [`Dockerfile`](Dockerfile), from the repository root:
  `docker build -f proxy-envoy-grpc/Dockerfile .`. It runs as uid 10001 and
  serves on `/run/rail/ext.sock`.
- **Envoy's config:** start from [`envoy.yaml`](envoy.yaml), the reference
  config. It holds no Rail configuration: the protected hosts and the ticket
  are this service's alone. It listens on 15001 and expects the socket at
  `/run/rail/ext.sock`.
- **Sharing the socket:** mount one volume at `/run/rail` in both containers.
  The socket is `0660`, so Envoy must run as uid 10001 or in group 10001 (the
  official Envoy image takes `ENVOY_GID=10001`).
- **Health:** `python -m proxy.envoy_grpc.probe_health` asks the service over
  its socket, prints its status as JSON, and exits 0 when it answers, whether
  or not a ticket is held. Use it as an exec probe: the service has no TCP
  port, since anything on localhost in a pod is reachable by the agent.

[`e2e/envoy-grpc`](../e2e/envoy-grpc/compose.yml) runs all of this.

From source: `make init`, then `uv run python -m proxy.envoy_grpc` with the
variables below.

## Configuration

It reads the variables every interface shares: see
[proxy-core's configuration](../proxy-core/README.md#configuration).

#### `RAIL_PROXY_PROTECTED_HOSTS`

Comma-separated entries, each a host with an optional scheme and port:
`mcp.example.com`, `mcp.example.com:8443`, `http://gateway.internal:8080`.

- HTTPS unless the entry says `http://`.
- The entry's port if it names one, else the agent's.
- One entry per host. A credential, a path, a query or a fragment is refused.

Required when `RAIL_PLUGIN_ENABLED` is on.

#### `RAIL_PROXY_EXT_SOCKET`

The unix socket Envoy calls. Defaults to `/run/rail/ext.sock`.
