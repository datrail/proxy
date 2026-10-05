# The end-to-end stacks

One stack per interface. Each has proxies, a stubbed Rail Center, a stubbed MCP
upstream and a driver that asserts what actually crossed the wire.

```sh
make e2e              # every stack in turn
make e2e-standalone   # one stack
```

The exit code is the result. Nothing outside this directory is needed — no Rail
Center, no gateway, no registry account — which is what makes this the
quickstart as well as the test.

```
e2e/
  shared/       what every stack uses: the stubs, the base services, the driver's helpers
  standalone/   the standalone proxy's stack
```

The stacks share their **fixtures** and their **assertion vocabulary**, not
their topology or their flow: each `compose.yml` and `driver.py` reads top to
bottom on its own.

## What it proves that the unit suite cannot

The unit suite drives the application in-process, through an ASGI transport and a mock
HTTP transport. That covers the behaviour thoroughly and cannot cover any of
this:

- the **image** runs — the entrypoint, the non-root user, the mounted config path
- a **real socket**: a real uvicorn, a real DNS name, a real TCP connection
- FastMCP's client completing a **real handshake over the wire**
- the ticket states side by side in **one network**, which is how they are
  actually told apart

State *transitions* — rotation, Rail Center going down, a ticket lapsing — are
the core's, and its unit tests cover them with a fake clock. A stack only shows
that each state, once reached, comes out on the wire as it should. That is why
each state is its own proxy, standing still, rather than one proxy driven from
state to state.

## Shared

### `services.yml`

The WireMock base service and `rail-center`, pulled into each stack with
`extends: {file: ../shared/services.yml, service: …}`. YAML anchors don't cross
files; `extends` does.

### Rail Center (`rc-mappings/`)

Matched on `sandbox_name`, so every state is configuration, not code:

| `sandbox_name` | Rail Center answers | The proxy reports |
|---|---|---|
| `e2e-sandbox` | a ticket, `e2e-opaque-token`, six hours from expiry | `x-rail: e2e-opaque-token` |
| anything else | an empty list — authoritative, not an error | `x-rail-status: not-found` |

`--global-response-templating` is what keeps `expires_at` six hours ahead of
now. A hardcoded stamp would pass today
and fail silently on whatever day it went past. The helper carries
`timezone='UTC'` for a second reason: the `Z` in its format string is a literal
rather than an offset, so without it the stamp renders in the container's local
zone while claiming to be UTC. Any zone west of UTC−6 then hands the proxy a
ticket that already expired, and the suite fails naming the proxy.
`rail-center` runs at `TZ: Pacific/Honolulu` for that reason: on a default-UTC
JVM the argument is unobservable and dropping it changes nothing, so the suite
runs where dropping it fails.

### The MCP upstream (`mcp-mappings/`)

The upstream is configuration, not code. FastMCP's client accepts
`Content-Type: application/json` and does not require SSE framing, so four
body-matched stubs answer `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`. Two more are needed and are easy to miss: the
client opens `GET /mcp` (405 is a valid answer) and sends `DELETE /mcp` when it
closes. Unmatched, either one is a request the journal records as an error.

No session state is needed. Returning `Mcp-Session-Id` once on `initialize` is
enough — the client echoes it on everything after.

### The driver's helpers (`lib.py`)

`count`, `reset_journal`, `unmatched`, the header matchers, `drive` and
`expect`. Standard library only: each stack's driver runs in a stock `python`
image with `lib.py` mounted beside it.

The assertions are WireMock's request journal, not log scraping. The question
is what crossed the wire, and only the journal answers it. Three details cost
real time to find, so they are written down rather than rediscovered:

- **Every admin call fails loudly on a non-2xx answer.** The journal reset
  moved between WireMock majors — `POST /__admin/requests/reset` is gone in
  3.x, `DELETE /__admin/requests` replaced it. A 404 there that went unnoticed
  leaves the reset doing nothing, and the next assertion counts headers a
  *previous* container attached. It passes, for the wrong reason. `urlopen`
  raises on it, which stops the run.
- **Presence is `{"matches": ".*"}`.** There is no `present` matcher, and
  `{"absent": false}` is not one — it reads as no constraint at all, so every
  count returns the total and every absence assertion passes.
- **A missing stub is invisible to every count.** The counts are of requests a
  stub *matched*, and the `GET` and `DELETE` above are matched by stubs nothing
  else touches. `unmatched` reads the unmatched journal, so each proxy also
  asserts that every request it caused was answered.

## Standalone (`standalone/`)

```sh
docker compose -f e2e/standalone/compose.yml up --build --abort-on-container-exit --exit-code-from driver
```

| Service | Configuration | What the upstream should see |
|---|---|---|
| `proxy` | registered as `e2e-sandbox` | `x-rail: e2e-opaque-token` |
| `proxy-unregistered` | a sandbox name Rail Center does not know | `x-rail-status: not-found`, no identity |
| `proxy-passthrough` | no RailXia configuration at all | neither header |
| `image-user` | the same image, sleeping | nothing: its healthcheck asserts the uid, and the driver waits on it |

The last proxy is the one worth understanding: it is *not* the fail-closed
path. A proxy with no control plane and a proxy whose ticket lapsed are
different states, and the difference is what lets a gateway tell them apart.

Every proxy is driven with a forged `x-rail: forged-by-the-sandbox` and a
forged `Authorization`, and the driver asserts neither reaches the upstream.
The proxy is the boundary; an identity it did not issue does not cross it.

### Not covered here

`RAIL_AUTH_MODE=bearer` — the mode the proxy uses to authenticate *to* Rail
Center. Proving it over this stack's plaintext `http` would need
`RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL=true`, since the proxy refuses to send a
credential in the clear. Putting that flag in the quickstart would teach it as
normal, so `bearer` is left to the unit suite, where it is covered without one.
