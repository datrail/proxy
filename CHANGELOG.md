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
- `spec/ticket-fetch.schema.json`, pinning the fetch response this proxy parses.
- `e2e/`: a stack in containers per interface, asserting what reaches an upstream
  in each ticket state: held, not found, expired, issuer unreachable and off.
- `proxy-envoy-grpc`, the proxy as a service Envoy calls on each request
  (ext_authz, over a unix socket), with a reference Envoy config. It attaches
  the same `x-rail` headers to protected hosts and leaves the agent's other
  headers as sent. Its image is `ghcr.io/datrail/proxy-envoy-grpc`, released
  from the same tags as `ghcr.io/datrail/proxy`.
- Container images with an SBOM. A signed build-provenance attestation is
  attached where the repository is public — attestation requires that or
  GitHub Enterprise Cloud — and a release that cannot produce one warns
  rather than failing.

### Changed

- **The source is a uv workspace**: `proxy-core`, and a package per interface, `proxy-standalone` and `proxy-envoy-grpc` (importing as `proxy.core`, `proxy.standalone` and `proxy.envoy_grpc`), so each image installs only what it runs. The `ghcr.io/datrail/proxy` image is unchanged — same name, user, port, environment and installed packages — except that its command is now `python -m proxy.standalone` and its Python environment lives in `/app/.venv`, first on `PATH`. The mount point for the bridge file is still `/app/standalone/bridge.yaml`. Every dependency, transitive ones included, is now pinned by `uv.lock`, and running from source needs Python 3.12.
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

### Removed

- `RAIL_TICKET_MODE`, which `v0.1.0` shipped and its `.env.example` carried. An
  environment still giving it a value is refused at startup, in a message naming
  `RAIL_PLUGIN_ENABLED` as what to set instead; left empty it is ignored.
  Upgrading from `v0.1.0` means replacing the variable rather than dropping it,
  since the proxy attaches nothing until `RAIL_PLUGIN_ENABLED=true` says so.
