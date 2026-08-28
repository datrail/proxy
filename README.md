# DatRail Proxy

An MCP proxy that stands between an agent and the server it calls, so the
sandbox never holds the credential that identifies it. It receives the agent's
MCP calls, forwards them to the upstreams its config names, and re-exposes
their tools namespaced.

It does not yet attach the `x-rail` ticket — the identity header the rest of
DatRail relies on. What it does today is refuse to carry one the agent supplies:
the proxy is the boundary, and an identity it did not issue does not cross it.

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Sending a change:
[CONTRIBUTING.md](CONTRIBUTING.md). Taking part:
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security reports go to
[SECURITY.md](SECURITY.md) rather than a public issue.
