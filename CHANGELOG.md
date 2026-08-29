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
- `RAIL_TICKET_MODE`, cross-checked against the Rail Center variables in both
  directions, so a deployment that meant to attach an identity and lost its
  configuration stops rather than forwarding unstamped.
- `GET /health`, reporting the mode and what is held — a fingerprint and an
  expiry, never the ticket and never the issuer's address.
- `spec/ticket-fetch.schema.json`, pinning the fetch response this proxy parses.
- `e2e/`: the stack in containers, asserting what reaches an upstream.
- Container images with an SBOM. A signed build-provenance attestation is
  attached where the repository is public — attestation requires that or
  GitHub Enterprise Cloud — and a release that cannot produce one warns
  rather than failing.
