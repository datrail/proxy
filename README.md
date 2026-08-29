# DatRail Proxy

An MCP proxy that stands between an agent and the server it calls, so the
sandbox never holds the credential that identifies it. It receives the agent's
MCP calls, forwards them to the upstreams its config names, and re-exposes
their tools namespaced.

It refuses to carry an `x-rail` ticket the agent supplies: the proxy is the
boundary, and an identity it did not issue does not cross it. The one it
attaches instead is its own, fetched from a Rail Center the sandbox cannot
reach.

Point `RAIL_CENTER_URL`, `RAIL_HOST_ID` and `RAIL_SANDBOX_NAME` at a Rail
Center and the proxy fetches a ticket for `(host, sandbox)`, keeps it fresh,
and puts it on everything it forwards. The response it expects is pinned in
[spec/ticket-fetch.schema.json](spec/ticket-fetch.schema.json); every setting
is listed in
[fastmcp_proxy/bridge.yaml.example](fastmcp_proxy/bridge.yaml.example).

**`RAIL_TICKET_MODE` decides what goes out**, and it defaults to `enforce`. It
is cross-checked against the Rail Center variables both ways: a mode that
attaches with nothing to fetch from stops at startup rather than forwarding
unstamped, and so does `none` beside a Rail Center that is configured. Set it to
`none`, with those unset, to run without one.

| Mode | Configuration | Outbound headers |
|---|---|---|
| `none` | no Rail Center | neither header |
| `observe` / `enforce`, ticket held | Rail Center answered | `x-rail: <ticket>` |
| `observe` / `enforce`, no valid ticket | Rail Center down, or holds none | `x-rail-status: not-found` \| `expired` \| `issuer-unreachable` |

The third row is the fail-closed path: the call still goes out, without an
identity and saying why. `XRailInjector` is where that decision is written down.
`observe` and `enforce` behave identically in this component.

`GET /health` reports the process is up, the mode it is in, and — where there is
one to report — what it holds by way of a ticket: a fingerprint, an expiry and
why there is none, never the value and never the Rail Center's address. It
answers 200 whether or not a ticket is held, because failing closed is a
designed state rather than a fault.

Published as a container image at `ghcr.io/datrail/proxy`, with an SBOM and a
signed build-provenance attestation on every release:

```
gh attestation verify oci://ghcr.io/datrail/proxy:latest --owner datrail
```

It listens on `0.0.0.0:8091`, serving `POST /mcp` and `GET /health`, and reads
its upstream list from a `bridge.yaml` you mount:

Forwarding only, with no control plane:

```
docker run -p 8091:8091 \
  -v ./bridge.yaml:/app/fastmcp_proxy/bridge.yaml:ro \
  -e RAIL_TICKET_MODE=none \
  ghcr.io/datrail/proxy:latest
```

Attaching an identity:

```
docker run -p 8091:8091 \
  -v ./bridge.yaml:/app/fastmcp_proxy/bridge.yaml:ro \
  -e RAIL_CENTER_URL=https://rail-center.example.com \
  -e RAIL_HOST_ID=host-01 \
  -e RAIL_SANDBOX_NAME=agent-one \
  ghcr.io/datrail/proxy:latest
```

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Sending a change:
[CONTRIBUTING.md](CONTRIBUTING.md). Taking part:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security reports go to
[SECURITY.md](SECURITY.md) rather than a public issue.
