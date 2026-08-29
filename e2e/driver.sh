#!/bin/sh
# Drives each proxy and asserts what reached the upstream. The assertions are
# WireMock's request journal, not log scraping: the question is what crossed
# the wire, and the journal is the only thing that answers it.
set -eu

UPSTREAM=http://upstream:8080
ACCEPT='Accept: application/json, text/event-stream'
JSON='Content-Type: application/json'
fails=0

# Every admin call carries -f, and that is load-bearing rather than tidy. The
# journal reset moved between WireMock majors — `POST /__admin/requests/reset`
# is gone in 3.x, `DELETE /__admin/requests` replaced it — and without -f a 404
# there is silent. A reset that quietly did nothing leaves the previous proxy's
# traffic in the journal, so the next assertion counts headers that another
# container attached and passes for the wrong reason.
#
# The count is read by stripping whitespace first: WireMock pretty-prints
# `"count" : 7`, so a pattern written against `"count":7` never matches.
count() {
  curl -sf -H "$JSON" -X POST "$UPSTREAM/__admin/requests/count" -d "$1" \
    | tr -d ' \n' | sed -n 's/.*"count":\([0-9]*\).*/\1/p'
}

reset_journal() { curl -sf -X DELETE "$UPSTREAM/__admin/requests" >/dev/null; }

# Requests no stub answered, as 0 or 1 so `expect ... none` reads it. Every
# other assertion here counts requests the upstream *matched*, and a stub that
# went missing is invisible to all of them: the client's `GET /mcp` and its
# closing `DELETE /mcp` are answered by stubs nothing else touches, so deleting
# either changes no count at all. There is no count endpoint for the unmatched
# journal, so the array is read directly — pretty-printed `"requests" : [ ]`
# becomes `"requests":[]` once whitespace is stripped, and anything else is at
# least one request that arrived with no stub to answer it.
unmatched() {
  if curl -sf "$UPSTREAM/__admin/requests/unmatched" | tr -d ' \n' \
    | grep -qF '"requests":[]'; then echo 0; else echo 1; fi
}

# The proxy is stateless — it issues no session id — so a call needs no
# handshake state carried between requests. `initialize` is sent anyway
# because that is what a real client does.
drive() {
  host=$1; shift
  curl -sf -o /dev/null -X POST "http://$host:8091/mcp" -H "$ACCEPT" -H "$JSON" "$@" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"e2e-driver","version":"1"}}}'
  # The tool call's body is read rather than discarded, and `"isError":false` is
  # the leg that does the work. fastmcp does not hand the upstream's JSON-RPC
  # error back as an error: it converts it into a *successful* result whose
  # content carries `"isError":true`, so matching `"result"` alone passes on a
  # call that failed outright while every header assertion below still counts the
  # POST that carried the failure. `delivery_track_package` is the mount name in
  # bridge.yaml joined to the tool name the upstream lists, a coupling across
  # three files: break any leg of it and this is what says so.
  body=$(curl -sf -X POST "http://$host:8091/mcp" -H "$ACCEPT" -H "$JSON" "$@" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delivery_track_package","arguments":{"id":"pkg-1"}}}')
  case "$body" in
    *'"result"'*'"isError":false'*) printf '  ok    the tool call returns a result\n' ;;
    *) printf '  FAIL  the tool call returns a result — got %s\n' "$body"
       fails=$((fails + 1)) ;;
  esac
}

expect() {
  what=$1 want=$2 got=$3
  if [ "$want" = "some" ]; then
    [ "${got:-0}" -ge 1 ] && printf '  ok    %s (%s)\n' "$what" "$got" && return 0
  else
    [ "${got:-x}" = "0" ] && printf '  ok    %s\n' "$what" && return 0
  fi
  printf '  FAIL  %s — got %s, wanted %s\n' "$what" "${got:-<none>}" "$want"
  fails=$((fails + 1))
}

with_xrail='{"method":"POST","url":"/mcp","headers":{"x-rail":{"equalTo":"e2e-opaque-token"}}}'
# Presence is `matches: ".*"`. There is no `present` matcher, and `absent:
# false` is not one either — it reads as no constraint at all, so every count
# comes back as the total and every absence assertion passes.
any_xrail='{"method":"POST","url":"/mcp","headers":{"x-rail":{"matches":".*"}}}'
any_status='{"method":"POST","url":"/mcp","headers":{"x-rail-status":{"matches":".*"}}}'
not_found='{"method":"POST","url":"/mcp","headers":{"x-rail-status":{"equalTo":"not-found"}}}'
forged='{"method":"POST","url":"/mcp","headers":{"x-rail":{"equalTo":"forged-by-the-sandbox"}}}'
# Presence, not the forged value: the proxy attaches no `authorization` of its
# own, so any at all upstream came from the caller.
any_auth='{"method":"POST","url":"/mcp","headers":{"authorization":{"matches":".*"}}}'
any_post='{"method":"POST","url":"/mcp"}'

# All three proxies are driven with it, not just the pass-through one.
FORGED='x-rail: forged-by-the-sandbox'
# `authorization` is the second header the boundary strips, named alongside
# `x-rail` in the comment on `forward_incoming_headers`, and it is driven here
# for a reason the forged `x-rail` cannot cover: no injector writes over it. On
# the registered proxy the injector sets `x-rail` to the ticket regardless of
# what was forwarded, so with forwarding back on the forged `x-rail` still never
# reaches the upstream and only this assertion sees the breach.
FORGED_AUTH='Authorization: Bearer forged-by-the-sandbox'

echo "registered proxy — holds a ticket and attaches it"
reset_journal; drive proxy -H "$FORGED" -H "$FORGED_AUTH"
expect "the ticket reaches the upstream"        some "$(count "$with_xrail")"
expect "no status header alongside it"          none "$(count "$any_status")"
expect "the sandbox's own x-rail never crosses" none "$(count "$forged")"
expect "no authorization crosses"               none "$(count "$any_auth")"
expect "every request matched a stub"           none "$(unmatched)"

echo "unregistered proxy — Rail Center holds no ticket for it"
reset_journal; drive proxy-unregistered -H "$FORGED" -H "$FORGED_AUTH"
expect "the call is still forwarded"            some "$(count "$any_post")"
expect "no identity is attached"                none "$(count "$any_xrail")"
expect "and it says why: not-found"             some "$(count "$not_found")"
expect "the sandbox's own x-rail never crosses" none "$(count "$forged")"
expect "no authorization crosses"               none "$(count "$any_auth")"
expect "every request matched a stub"           none "$(unmatched)"

echo "pass-through proxy — no control plane configured"
reset_journal; drive proxy-passthrough -H "$FORGED" -H "$FORGED_AUTH"
expect "the call is still forwarded"            some "$(count "$any_post")"
expect "neither header is attached"             none "$(count "$any_xrail")"
expect "not even a status header"               none "$(count "$any_status")"
expect "the sandbox's own x-rail never crosses" none "$(count "$forged")"
expect "no authorization crosses"               none "$(count "$any_auth")"
expect "every request matched a stub"           none "$(unmatched)"

echo
[ "$fails" -eq 0 ] && echo "e2e: all assertions passed" && exit 0
echo "e2e: $fails assertion(s) failed"; exit 1
