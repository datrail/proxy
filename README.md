# DatRail Proxy

An MCP proxy that stands between an agent and the server it calls, so the
sandbox never holds the credential that identifies it. It receives the agent's
MCP calls, forwards them to the upstreams its config names, and re-exposes
their tools namespaced.

It refuses to carry an `x-rail` ticket the agent supplies: the proxy is the
boundary, and an identity it did not issue does not cross it. It does not attach
one either — no request leaves here carrying the identity header the rest of
DatRail relies on.

Point `RAIL_CENTER_URL`, `RAIL_HOST_ID` and `RAIL_SANDBOX_NAME` at a Rail
Center and it fetches its own ticket once at startup, and logs what came back —
a fingerprint, never the ticket. That is a check on the configuration, not a
dependency: with those unset the proxy runs and forwards exactly as it
otherwise would. The response it expects is pinned in
[spec/ticket-fetch.schema.json](spec/ticket-fetch.schema.json); the rest of the
settings are listed in
[fastmcp_proxy/bridge.yaml.example](fastmcp_proxy/bridge.yaml.example).

Published as a container image at `ghcr.io/datrail/proxy`, with an SBOM and a
provenance attestation on every release. It listens on `0.0.0.0:8091`, serving
`POST /mcp` and `GET /health`, and reads its upstream list from a
`bridge.yaml` you mount:

```
docker run -p 8091:8091 \
  -v ./bridge.yaml:/app/fastmcp_proxy/bridge.yaml:ro \
  ghcr.io/datrail/proxy:latest
```

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Sending a change:
[CONTRIBUTING.md](CONTRIBUTING.md). Taking part:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security reports go to
[SECURITY.md](SECURITY.md) rather than a public issue.
