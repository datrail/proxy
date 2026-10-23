# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions correspond to published images at `ghcr.io/datrail/proxy`
and, from the first release that has it, `ghcr.io/datrail/proxy-envoy-grpc`.

## [Unreleased]

### Added

- An MCP proxy that mounts the upstreams named in its config and re-exposes
  their tools namespaced as `<name>_<tool>`.
- `x-rail` ticket handling: fetched from Rail Center for `(host, sandbox)`,
  refreshed ahead of expiry, and attached to every forwarded call. Where no
  valid ticket is held the call still goes out carrying `x-rail-status`.
- `RAIL_PLUGIN_ENABLED`, whether this proxy talks to a Rail Center at all. Off
  by default, parsed strictly, and cross-checked against the Rail Center
  variables in both directions — so a deployment that meant to attach an
  identity and lost its configuration stops rather than forwarding unstamped.
- `schema_version` on the config file, carried so a shape change is reported
  rather than half-read. It is this file's version and not the policy bundle's:
  the major is compared, a file omitting it is read as `1.0` with a warning, and
  an unreadable major refuses to start.
- `GET /health`, reporting whether the plugin is on and what is held — a
  fingerprint and an expiry, never the ticket and never the issuer's address.
- `schemas/ticket-fetch.schema.json`, pinning the fetch response this proxy
  parses.
- `e2e/`: a stack in containers per interface, asserting what reaches an upstream
  in each ticket state: held, not found, expired, issuer unreachable and off.
- `proxy-envoy-grpc`, the proxy as a service Envoy calls on each request
  (ext_authz or ext_proc, over a unix socket), with an example Envoy config
  for each in `e2e/`. It attaches the same `x-rail` headers to protected hosts
  and leaves the agent's other headers as sent. Its image is
  `ghcr.io/datrail/proxy-envoy-grpc`, released from the same tags as
  `ghcr.io/datrail/proxy`.
- Judging requests, in `proxy-core`, run by `proxy-envoy-grpc` over either
  callout. A request is judged on its method, URL and headers, and on its
  body when a rule needs it, read in the mode Envoy is configured with and
  decoded (`gzip`) and parsed (JSON) under a cap. A refused request gets `403`.
  Until rules exist every request is allowed. A body that can't be judged
  (not sent, truncated, too large, malformed…) is refused unless
  `RAIL_PROXY_ALLOW_UNREADABLE_BODY` names its reason, and a failure while
  judging is refused unless `RAIL_PROXY_ON_JUDGE_FAILURE=allow`. The bounds are
  `RAIL_PROXY_MAX_BODY_BYTES`, `RAIL_PROXY_BODY_BUDGET_BYTES` and
  `RAIL_PROXY_BODY_TIMEOUT_SECONDS`.
- Container images with an SBOM. A signed build-provenance attestation is
  attached where the repository is public — attestation requires that or
  GitHub Enterprise Cloud — and a release that cannot produce one warns
  rather than failing.

### Changed

- **The source is a uv workspace**: `proxy-core`, and a package per interface, `proxy-standalone` and `proxy-envoy-grpc` (importing as `proxy.core`, `proxy.standalone` and `proxy.envoy_grpc`), so each image installs only what it runs. The `ghcr.io/datrail/proxy` image is unchanged — same name, user, port and installed packages — except that its command is now `python -m proxy.standalone` and its Python environment lives in `/app/.venv`, first on `PATH`. The mount point for the bridge file is still `/app/standalone/bridge.yaml`. Every dependency, transitive ones included, is now pinned by `uv.lock`, and running from source needs Python 3.12.
- **proxy-standalone forwards the agent's own headers**, as proxy-envoy-grpc
  does. Each upstream request carries the headers of the agent request that
  caused it, `Authorization` included, except any `x-rail` or `x-rail-*`
  header, any with `_` in its name, and those about the agent's own
  connection to the proxy (`mcp-*`, `last-event-id`, `accept-encoding`). It
  used to forward none, so an upstream now sees the agent's own credentials
  for it.
- **Log lines are named after the module that wrote them**, e.g.
  `proxy.standalone.server` and `proxy.core.xrail_auth`, in place of
  `fastmcp_proxy` and `fastmcp_proxy.xrail`. Anything that filters this proxy's
  output by logger name needs the new names.

- **proxy-envoy-grpc's README tells a deployment to bind Envoy's listener to
  loopback.** Envoy attaches the agent's ticket to whatever reaches it, and the
  e2e config it points to binds `0.0.0.0`, which a sidecar copying it exposes to
  every container on the agent's network.
- **The `ghcr.io/datrail/proxy` image no longer sets `RAIL_PROXY_BIND`,
  `RAIL_PROXY_PORT` and `RAIL_PROXY_CONFIG_FILE`.** The values it set are the
  defaults, `0.0.0.0`, `8091` and `standalone/bridge.yaml` under its working
  directory `/app`, so behaviour is unchanged unless the container is started
  in another working directory; `docker inspect` no longer lists them.

### Removed

- `RAIL_TICKET_MODE`, which `v0.1.0` shipped and its `.env.example` carried. An
  environment still giving it a value is refused at startup, in a message naming
  `RAIL_PLUGIN_ENABLED` as what to set instead; left empty it is ignored.
  Upgrading from `v0.1.0` means replacing the variable rather than dropping it,
  since the proxy attaches nothing until `RAIL_PLUGIN_ENABLED=true` says so.

### Security

- Both images ship certifi 2026.10.19, which drops the "emSign ECC Root CA -
  G3" root, an issuer of intermediates whose keys were compromised.
  `ghcr.io/datrail/proxy` ships opentelemetry-api 1.45.1, which bounds how much
  of a `tracestate` header it parses; `proxy-envoy-grpc` doesn't include it.
