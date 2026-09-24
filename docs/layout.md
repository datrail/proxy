# Proxy and Gateway layout decisions

DR-61 and DR-62 make these decisions once for both repositories. They apply to
DatRail Proxy and DatRail Gateway equally; changing one requires a coordinated
change to this file in both repositories.

## Shared decisions

1. **Group plugins by extension mechanism, not cloud vendor.** A future
   `plugins/ext-proc/` implementation can serve both Google Service Extensions
   and Istio instead of being copied under two provider names. Directory names
   are lowercase mechanism names. The `plugins/` tree does not land until the
   first real plugin does.
2. **Each plugin owns its toolchain.** A plugin directory is an independently
   buildable and testable package with its own runtime manifest. Repository-wide
   checks may orchestrate those builds, but neither the Python standalone host
   nor another plugin dictates a single build or language for all plugins.
3. **The two repositories have separate, responsibility-specific cores.** Proxy
   core obtains and injects an opaque ticket; Gateway core parses it and makes an
   enforcement decision. They share no runtime library and duplicate no common
   enforcement implementation. Their shared boundary is the x-rail wire
   contract, which keeps Proxy from learning claims it must treat opaquely.
4. **x-rail-spec changes first, consumers follow explicitly.** Both cores
   currently target x-rail `v1.0` at `datrail/x-rail-spec` commit
   `1282f71495d9b585b6562255f5194f0754e3d586`. A wire change lands there with a
   new version before coordinated Proxy and Gateway PRs update this pin and
   their local conformance tests. Component release versions do not version the
   wire contract.

## Dependency direction

```text
standalone/ ──> core/ <── plugins/<mechanism>/
```

`core/` must never import `standalone/` or `plugins/`. Each repository pins
that rule with an architecture test. A plugin-to-core interface will be
versioned once in `core/` when the first plugin requires it; DR-61 and DR-62 do
not pre-design that interface.

## Standalone implementations

- Proxy retains the implementation formerly at `fastmcp_proxy/proxy.py`, now
  `standalone/server.py`. It is the implementation the Docker image ran and the
  one carrying the current ticket rotation, fail-closed, redaction, and config
  behavior. The historical `proxy_v2.py` parallel experiment on port 8092 is
  not retained: it was never the image entrypoint and would preserve a second
  host path that must be hardened in lockstep.
- Gateway retains its existing FastMCP/Starlette host, now
  `standalone/server.py`; its parsing and policy-decision code moves to
  `core/` without changing the wire or runtime behavior.

Repository names and the externally published `component_kind` values remain
`proxy` and `gateway`.
