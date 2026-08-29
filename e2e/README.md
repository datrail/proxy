# The end-to-end stack

Three proxies, a stubbed Rail Center, a stubbed MCP upstream and a driver that
asserts what actually crossed the wire.

```sh
docker compose -f e2e/compose.yml up --build --abort-on-container-exit --exit-code-from driver
```

The exit code is the result. Nothing outside this directory is needed — no Rail
Center, no gateway, no registry account — which is what makes this the
quickstart as well as the test.

## What it proves that the unit suite cannot

`tests/` drives the application in-process, through an ASGI transport and a mock
HTTP transport. That covers the behaviour thoroughly and cannot cover any of
this:

- the **image** runs — the entrypoint, the non-root user, the mounted config path
- a **real socket**: a real uvicorn, a real DNS name, a real TCP connection
- FastMCP's client completing a **real handshake over the wire**
- the three ticket states side by side in **one network**, which is how they are
  actually told apart

## The three proxies

| Service | Configuration | What the upstream should see |
|---|---|---|
| `proxy` | registered as `e2e-sandbox` | `x-rail: e2e-opaque-token` |
| `proxy-unregistered` | a sandbox name Rail Center does not know | `x-rail-status: not-found`, no identity |
| `proxy-passthrough` | `RAIL_TICKET_MODE=none` | neither header |

The third is the one worth understanding: it is *not* the fail-closed path. A
proxy with no control plane and a proxy whose ticket lapsed are different
states, and the difference is what lets a gateway tell them apart.

The driver also sends its own `x-rail: forged-by-the-sandbox` and asserts it
never reaches the upstream. The proxy is the boundary; an identity it did not
issue does not cross it.

## Why WireMock is enough

The upstream is configuration, not code. FastMCP's client accepts
`Content-Type: application/json` and does not require SSE framing, so four
body-matched stubs answer `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`. Two more are needed and are easy to miss: the
client opens `GET /mcp` (405 is a valid answer) and sends `DELETE /mcp` when it
closes. Unmatched, either one is a request the journal records as an error.

No session state is needed. Returning `Mcp-Session-Id` once on `initialize` is
enough — the client echoes it on everything after.

`--global-response-templating` is what keeps `expires_at` six hours ahead of
now. A hardcoded stamp would pass today and fail silently on whatever day it
went past. The helper carries `timezone='UTC'` for a second reason: the `Z` in
its format string is a literal rather than an offset, so without it the stamp
renders in the container's local zone while claiming to be UTC. Any zone west
of UTC−6 then hands the proxy a ticket that already expired, and the suite
fails naming the proxy. `rail-center` runs at `TZ: Pacific/Honolulu` for that
reason: on a default-UTC JVM the argument is unobservable and dropping it
changes nothing, so the suite runs where dropping it fails.

## Three things about the assertions

They are WireMock's request journal, not log scraping. The question is what
crossed the wire, and only the journal answers it. Three details cost real time
to find, so they are written down rather than rediscovered:

- **Every admin call carries `curl -f`.** The journal reset moved between
  WireMock majors — `POST /__admin/requests/reset` is gone in 3.x, `DELETE
  /__admin/requests` replaced it. Without `-f` the 404 is silent, the reset
  quietly does nothing, and the next assertion counts headers a *previous*
  container attached. It passes, for the wrong reason.
- **Presence is `{"matches": ".*"}`.** There is no `present` matcher, and
  `{"absent": false}` is not one — it reads as no constraint at all, so every
  count returns the total and every absence assertion passes.
- **The count is read with whitespace stripped.** WireMock pretty-prints
  `"count" : 7`, so a pattern written against `"count":7` never matches.

## Not covered here

`RAIL_AUTH_MODE=bearer` — the mode the proxy uses to authenticate *to* Rail
Center. Proving it over this stack's plaintext `http` would need
`RAIL_PROXY_ALLOW_INSECURE_CREDENTIAL=true`, since the proxy refuses to send a
credential in the clear. Putting that flag in the quickstart would teach it as
normal, so `bearer` is left to the unit suite, where it is covered without one.
