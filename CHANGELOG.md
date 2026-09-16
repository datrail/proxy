# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions correspond to published images at `ghcr.io/datrail/proxy`.

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
- `e2e/`: the stack in containers, asserting what reaches an upstream.
- Container images with an SBOM. A signed build-provenance attestation is
  attached where the repository is public — attestation requires that or
  GitHub Enterprise Cloud — and a release that cannot produce one warns
  rather than failing.

### Removed

- `RAIL_TICKET_MODE`, which `v0.1.0` shipped and its `.env.example` carried. An
  environment still giving it a value is refused at startup, in a message naming
  `RAIL_PLUGIN_ENABLED` as what to set instead; left empty it is ignored.
  Upgrading from `v0.1.0` means replacing the variable rather than dropping it,
  since the proxy attaches nothing until `RAIL_PLUGIN_ENABLED=true` says so.
