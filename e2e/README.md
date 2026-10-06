# The end-to-end stacks

One stack per interface. Each has proxies, a stubbed Rail Center, a stubbed MCP
upstream and a driver that asserts what actually crossed the wire.

```sh
make e2e              # every stack in turn
make e2e-standalone   # one stack
```

The result is the `driver` container's exit code, which `--exit-code-from`
passes through `docker compose up` and `make`. Nothing outside this directory
is needed — no Rail Center, no gateway, no registry account — which is what
makes this the quickstart as well as the test.

```
e2e/
  shared/       what every stack uses: the stubs, the base services, the driver's helpers
  standalone/   the standalone proxy's stack
```

## What it proves that the unit suite cannot

The unit suites drive each interface in-process, with the network mocked. They
cannot show:

- the **image** runs — the entrypoint, the non-root user, the mounted config
- a **real socket**: a real server, a real DNS name, a real TCP connection
- an MCP client completing a **real handshake over the wire**
- the ticket states side by side in **one network**, which is how they are
  actually told apart

State transitions (rotation, expiry, Rail Center going down) are covered by the
core's unit tests, so each state here is its own proxy, standing still.

## Shared

Rail Center's stubs (`rc-mappings/`) match on `sandbox_name`:

| `sandbox_name` | Rail Center answers | The proxy reports |
|---|---|---|
| `e2e-sandbox` | a ticket, `e2e-opaque-token`, six hours from expiry | `x-rail: e2e-opaque-token` |
| `e2e-expired` | a ticket whose `expires_at` was an hour ago | `x-rail-status: expired` |
| `e2e-issuer-down` | a 500 | `x-rail-status: issuer-unreachable` |
| anything else | an empty list — authoritative, not an error | `x-rail-status: not-found` |

`expires_at` is templated relative to now, so the fixtures don't rot.

The MCP upstream's stubs (`mcp-mappings/`) answer `initialize`,
`notifications/initialized`, `tools/list` and `tools/call`, plus two that are
easy to miss: the client opens `GET /mcp` (405 is a valid answer) and sends
`DELETE /mcp` when it closes.

The driver's assertions read WireMock's request journal, not logs. The helpers
are in `lib.py`, standard library only, and its comments record the WireMock
pitfalls they guard against.

## Standalone

| Service | Configuration | What the upstream should see |
|---|---|---|
| `proxy` | registered as `e2e-sandbox` | `x-rail: e2e-opaque-token` |
| `proxy-unregistered` | a sandbox name Rail Center does not know | `x-rail-status: not-found`, no identity |
| `proxy-expired` | `e2e-expired`: the only ticket has lapsed | `x-rail-status: expired`, no identity |
| `proxy-issuer-down` | `e2e-issuer-down`: Rail Center answers 500 | `x-rail-status: issuer-unreachable`, no identity; it still starts and reports healthy |
| `proxy-passthrough` | sets nothing: the plugin is off by default | neither header |
| `image-user` | the same image, sleeping | nothing: its healthcheck asserts the uid, and the driver waits on it |

`proxy-passthrough` is *not* the fail-closed path. A proxy with no control
plane and a proxy whose ticket lapsed are different states, and the difference
is what lets a gateway tell them apart.

Every proxy is driven with a forged `x-rail` and a forged `Authorization`, and
the driver asserts neither reaches the upstream.

### Not covered here

`RAIL_AUTH_MODE=bearer`, which authenticates the proxy *to* Rail Center. Over
this stack's plaintext `http` it would need
`RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL=true`, which the quickstart shouldn't
teach, so the unit suite covers it instead.
