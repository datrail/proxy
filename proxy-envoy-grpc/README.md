# proxy-envoy-grpc

`proxy.envoy_grpc`, an Envoy gRPC extension service. Envoy calls it on each
request, and it adds the `x-rail` headers to requests for protected hosts. See
the [main README](../README.md) for the whole.

Work in progress (DR-146): so far, only its settings.

## How it works

TODO

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
