"""Drives requests through each Envoy and asserts what reached each server."""

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from lib import (
    AGENT_HEADERS,
    ANY_XRAIL,
    FORGED,
    FORGED_STATUS,
    TIMEOUT_SECONDS,
    check,
    count,
    drive,
    equal_to,
    expect,
    finish,
    posts,
    present,
    reset_journal,
    the_agents_own_headers_cross,
    unmatched,
)

# The servers, by their journals' admin address.
UPSTREAM = "http://upstream:8080"
AGENT_PORT = "http://upstream-agent-port:8080"
UPSTREAM_443 = "http://upstream-443:8080"
PLAIN = "http://upstream-plain:8080"
OPEN = "http://open:8080"
EVIL = "http://evil:8080"


def envoy(name):
    return f"http://{name}:15001/mcp"


def run(via, target, base):
    """MCP through Envoy `via` to `target`, with the journal of `base` reset."""
    reset_journal(base)
    drive(envoy(via), {"Host": target, **AGENT_HEADERS}, tool="track_package")


def send(via, target, path="/mcp", method="POST", headers=None):
    """One request through Envoy `via`, not following redirects. Returns the
    status and body, whatever the status."""
    request = urllib.request.Request(
        f"http://{via}:15001{path}",
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"Host": target, "Content-Type": "application/json", **(headers or {})},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def ticketed(base, scheme="https"):
    expect(
        "the ticket arrives",
        "some",
        count(equal_to("x-rail", "e2e-opaque-token"), base),
    )
    expect(
        f"every request arrived over {scheme}",
        "some",
        count(posts(scheme=scheme), base),
    )
    other = "http" if scheme == "https" else "https"
    expect(f"none over {other}", "none", count(posts(scheme=other), base))
    expect("every request matched a stub", "none", unmatched(base))


print("protected host, registered — the ticket is attached, over HTTPS")
run("envoy", "upstream.test:8443", UPSTREAM)
ticketed(UPSTREAM)
expect(
    "no status header alongside it", "none", count(present("x-rail-status"), UPSTREAM)
)
expect("the sandbox's own x-rail never crosses", "none", count(FORGED, UPSTREAM))
the_agents_own_headers_cross(UPSTREAM)

for via, reason in [
    ("envoy-unregistered", "not-found"),
    ("envoy-expired", "expired"),
    ("envoy-issuer-down", "issuer-unreachable"),
]:
    print(f"protected host, {reason} — no identity, and the reason, over HTTPS")
    run(via, "upstream.test:8443", UPSTREAM)
    expect(
        "the call is forwarded over HTTPS",
        "some",
        count(posts(scheme="https"), UPSTREAM),
    )
    expect("no identity is attached", "none", count(ANY_XRAIL, UPSTREAM))
    expect(
        f"and it says why: {reason}",
        "some",
        count(equal_to("x-rail-status", reason), UPSTREAM),
    )
    expect(
        "the sandbox's own x-rail-status never crosses",
        "none",
        count(FORGED_STATUS, UPSTREAM),
    )
    the_agents_own_headers_cross(UPSTREAM)

print("protected host, plugin off — still over HTTPS, with neither header")
run("envoy-passthrough", "upstream.test:8443", UPSTREAM)
expect(
    "the call is forwarded over HTTPS", "some", count(posts(scheme="https"), UPSTREAM)
)
expect("neither header is attached", "none", count(ANY_XRAIL, UPSTREAM))
expect("not even a status header", "none", count(present("x-rail-status"), UPSTREAM))
the_agents_own_headers_cross(UPSTREAM)

print("unprotected host — forwarded as sent, with no x-rail header")
run("envoy", "open.test:8080", OPEN)
expect("the call is forwarded over HTTP", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
expect("no x-rail-status crosses", "none", count(present("x-rail-status"), OPEN))
the_agents_own_headers_cross(OPEN)

for target in ["upstream.test", "upstream.test:80", "upstream.test:9999"]:
    print(f"{target} — reached at its configured port, 8443")
    run("envoy", target, UPSTREAM)
    ticketed(UPSTREAM)

print("upstream-agent-port.test:9443 — a bare entry keeps the agent's port")
run("envoy", "upstream-agent-port.test:9443", AGENT_PORT)
ticketed(AGENT_PORT)

for target in ["upstream-443.test", "upstream-443.test:80"]:
    print(f"{target} — a bare entry with no port goes to HTTPS's 443")
    run("envoy", target, UPSTREAM_443)
    ticketed(UPSTREAM_443)

print("upstream-plain.test:8080 — an http:// entry is not upgraded")
run("envoy", "upstream-plain.test:8080", PLAIN)
ticketed(PLAIN, scheme="http")

print("upstream.test misresolved to evil — the certificate check refuses it")
# First, that evil serves TLS on 8443 at all, so a closed port can't pass for a
# refused certificate.
context = ssl.create_default_context(cafile="/certs/ca.crt")
with urllib.request.urlopen(
    "https://evil.test:8443/__admin/health", context=context, timeout=TIMEOUT_SECONDS
) as response:
    check("evil.test answers over HTTPS on 8443", response.status == 200)
# Then the request: Envoy connects to evil, which answers TLS, but evil sees no
# request, so the TLS handshake is what failed. Envoy's access log names the
# cause (CERTIFICATE_VERIFY_FAILED, expected upstream.test, got evil.test); its
# 503 body doesn't, and the driver can't read another container's log.
reset_journal(EVIL)
status, _ = send("envoy-misresolved", "upstream.test:8443")
check("the request fails with a 503", status == 503)
expect("evil sees no request", "none", count(posts(), EVIL))

print("no extension — forwarded with the x-rail headers removed, and no status")
reset_journal(OPEN)
drive(
    envoy("envoy-no-ext"),
    {"Host": "open.test:8080", **AGENT_HEADERS},
    tool="track_package",
)
expect("the call is forwarded", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
expect("no x-rail-status crosses", "none", count(present("x-rail-status"), OPEN))
the_agents_own_headers_cross(OPEN)
reset_journal(UPSTREAM)
status, _ = send("envoy-no-ext", "upstream.test")
check("a protected host is not routed: plain HTTP to port 80 fails", status == 503)
expect("upstream sees no request", "none", count(posts(), UPSTREAM))

print("a redirect from a protected host is not followed")
stub = {
    "request": {"method": "GET", "urlPath": "/redirect"},
    "response": {
        "status": 302,
        "headers": {"Location": "http://open.test:8080/followed"},
    },
}
urllib.request.urlopen(
    urllib.request.Request(
        f"{UPSTREAM}/__admin/mappings",
        data=json.dumps(stub).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    ),
    timeout=TIMEOUT_SECONDS,
).read()
reset_journal(OPEN)
status, _ = send("envoy", "upstream.test:8443", path="/redirect", method="GET")
check("the agent gets the 302", status == 302)
expect("Envoy did not follow it", "none", count({"urlPath": "/followed"}, OPEN))

print("over ext_proc — protected host, registered: the ticket, over HTTPS")
run("envoy-ext-proc", "upstream.test:8443", UPSTREAM)
ticketed(UPSTREAM)
expect(
    "no status header alongside it", "none", count(present("x-rail-status"), UPSTREAM)
)
expect("the sandbox's own x-rail never crosses", "none", count(FORGED, UPSTREAM))
the_agents_own_headers_cross(UPSTREAM)

for target, base, scheme in [
    ("upstream.test", UPSTREAM, "https"),
    ("upstream-agent-port.test:9443", AGENT_PORT, "https"),
    ("upstream-443.test", UPSTREAM_443, "https"),
    ("upstream-plain.test:8080", PLAIN, "http"),
]:
    print(f"over ext_proc — {target}: routed as over ext_authz")
    run("envoy-ext-proc", target, base)
    ticketed(base, scheme=scheme)

print("over ext_proc — unprotected host, forwarded as sent")
run("envoy-ext-proc", "open.test:8080", OPEN)
expect("the call is forwarded over HTTP", "some", count(posts(scheme="http"), OPEN))
expect("no x-rail crosses", "none", count(ANY_XRAIL, OPEN))
the_agents_own_headers_cross(OPEN)

for via, callout in [("envoy-judged", "ext_authz"), ("envoy-ext-proc", "ext_proc")]:
    print(f"over {callout} — refused on its headers: a 403, and nothing goes out")
    reset_journal(UPSTREAM)
    status, body = send(via, "upstream.test:8443", headers={"x-e2e-judge": "deny"})
    check("the agent gets a 403", status == 403)
    check("with the fixed text", body == "request refused by policy\n")
    expect("upstream sees no request", "none", count(posts(), UPSTREAM))

    print(f"over {callout} — a body rule, with no body configured: refused as not sent")
    reset_journal(UPSTREAM)
    status, _ = send(via, "upstream.test:8443", headers={"x-e2e-judge": "body"})
    check("the agent gets a 403", status == 403)
    expect("upstream sees no request", "none", count(posts(), UPSTREAM))

print("both callouts in one chain — the second leaves the request as it was")
for target, base, scheme in [
    ("upstream.test:8443", UPSTREAM, "https"),
    ("upstream.test", UPSTREAM, "https"),
    ("upstream-agent-port.test:9443", AGENT_PORT, "https"),
    ("upstream-plain.test:8080", PLAIN, "http"),
]:
    run("envoy-variants", target, base)
    ticketed(base, scheme=scheme)
    expect("the sandbox's own x-rail never crosses", "none", count(FORGED, base))
    the_agents_own_headers_cross(base)


class _Recorder:
    """A plain HTTP server that keeps every byte it is sent, so a request cut
    short, or headers sent alone, still show. WireMock's journal only has whole
    requests."""

    def __init__(self, port):
        self._received = bytearray()
        self._lock = threading.Lock()
        server = socket.create_server(("0.0.0.0", port))
        threading.Thread(target=self._serve, args=(server,), daemon=True).start()

    def saw(self, text):
        with self._lock:
            return text.encode() in self._received

    def _serve(self, server):
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            ).start()

    def _handle(self, connection):
        with connection:
            pending = b""
            while data := connection.recv(65536):
                with self._lock:
                    self._received += data
                pending += data
                while (end := _request_end(pending)) is not None:
                    pending = pending[end:]
                    connection.sendall(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n")


def _request_end(data):
    """Where the first whole request in `data` ends, or None."""
    head_end = data.find(b"\r\n\r\n")
    if head_end < 0:
        return None
    head_end += 4
    head = data[:head_end].lower()
    if b"transfer-encoding: chunked" in head:
        # The last chunk, then any trailers, then a blank line.
        last = data.find(b"\r\n0\r\n", head_end - 2)
        end = -1 if last < 0 else data.find(b"\r\n\r\n", last + 2)
        return None if end < 0 else end + 4
    length = 0
    for line in head.split(b"\r\n"):
        if line.startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return head_end + length if len(data) >= head_end + length else None


def upload(port, path, parts):
    """A JSON body the judge reads, sent in `parts` a moment apart, through
    the ext_proc listener on `port`, to the recorder."""

    def slowly():
        for part in parts:
            time.sleep(0.3)
            yield part

    connection = http.client.HTTPConnection(
        "envoy-variants", port, timeout=TIMEOUT_SECONDS
    )
    try:
        connection.request(
            "POST",
            path,
            body=slowly(),
            headers={
                "Host": "driver:18080",
                "Content-Type": "application/json",
                "x-e2e-judge": "body",
            },
            encode_chunked=True,
        )
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def upload_with_trailers(port, path, parts):
    """As `upload`, with a trailer after the body, which `http.client` can't
    send."""
    with socket.create_connection(
        ("envoy-variants", port), timeout=TIMEOUT_SECONDS
    ) as connection:
        connection.sendall(
            f"POST {path} HTTP/1.1\r\n"
            "Host: driver:18080\r\n"
            "Content-Type: application/json\r\n"
            "x-e2e-judge: body\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Trailer: x-e2e-trailer\r\n"
            "\r\n".encode()
        )
        for part in parts:
            time.sleep(0.3)
            connection.sendall(b"%x\r\n%s\r\n" % (len(part), part))
        connection.sendall(b"0\r\nx-e2e-trailer: yes\r\n\r\n")
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, response.read().decode()


recorder = _Recorder(18080)
for mode, port in [
    ("buffered", 15011),
    ("buffered-partial", 15012),
    ("streamed", 15013),
    ("streamed-without-waiting", 15014),
    ("full-duplex", 15015),
]:
    print(f"ext_proc {mode} — a body the judge allows arrives whole")
    tool = f'"{mode}-tool"}}'.encode()
    status, _ = upload(port, f"/{mode}/allowed", [b'{"tool": ', tool])
    check("the agent gets the upstream's 200", status == 200)
    check(
        "the upstream gets the headers and the body",
        recorder.saw(f"/{mode}/allowed") and recorder.saw(f'"{mode}-tool"'),
    )

    print(f"ext_proc {mode} — a body the judge refuses: nothing reaches the upstream")
    status, body = upload(port, f"/{mode}/denied", [b'{"deny": ', b"true}"])
    check("the agent gets a 403", status == 403)
    check("with the fixed text", body == "request refused by policy\n")
    check(
        "not even the headers reach the upstream", not recorder.saw(f"/{mode}/denied")
    )

    if mode == "buffered-partial":
        # Trailers follow a body that no part ends: the same as one cut short.
        print(f"ext_proc {mode} — a whole body with trailers: refused as truncated")
        status, _ = upload_with_trailers(
            port, f"/{mode}/trailers", [b'{"tool": ', b'"t"}']
        )
        check("the agent gets a 403", status == 403)
        check("nothing reaches the upstream", not recorder.saw(f"/{mode}/trailers"))
        continue
    if mode.startswith("streamed"):
        # No part ends it, and Envoy sends the trailers only once it's answered.
        print(f"ext_proc {mode} — a body with trailers never ends: Envoy gives up")
        status, _ = upload_with_trailers(
            port, f"/{mode}/trailers", [b'{"tool": ', b'"t"}']
        )
        check("the agent gets a 504", status == 504)
        check("nothing reaches the upstream", not recorder.saw(f"/{mode}/trailers"))
        continue

    print(f"ext_proc {mode} — a body with trailers is judged")
    tool = f'"{mode}-trailed"}}'.encode()
    status, _ = upload_with_trailers(port, f"/{mode}/trailed", [b'{"tool": ', tool])
    check("the agent gets the upstream's 200", status == 200)
    check(
        "the upstream gets the headers and the body",
        recorder.saw(f"/{mode}/trailed") and recorder.saw(f'"{mode}-trailed"'),
    )
    status, _ = upload_with_trailers(
        port, f"/{mode}/trailed-denied", [b'{"deny": ', b"true}"]
    )
    check("one the judge refuses gets a 403", status == 403)
    check(
        "and nothing reaches the upstream",
        not recorder.saw(f"/{mode}/trailed-denied"),
    )

print("ext_proc buffered, trailers sent too — a body with trailers is judged")
status, _ = upload_with_trailers(15017, "/send-trailers", [b'{"tool": ', b'"st"}'])
check("the agent gets the upstream's 200", status == 200)
check(
    "the upstream gets the headers and the body",
    recorder.saw("/send-trailers") and recorder.saw('"st"'),
)

print("ext_authz with the body — a body the judge allows arrives whole")
status, _ = upload(15016, "/ext-authz/allowed", [b'{"tool": ', b'"authz-tool"}'])
check("the agent gets the upstream's 200", status == 200)
check(
    "the upstream gets the headers and the body",
    recorder.saw("/ext-authz/allowed") and recorder.saw('"authz-tool"'),
)

print("ext_authz with the body — a body the judge refuses: nothing goes out")
status, body = upload(15016, "/ext-authz/denied", [b'{"deny": ', b"true}"])
check("the agent gets a 403", status == 403)
check("with the fixed text", body == "request refused by policy\n")
check("nothing reaches the upstream", not recorder.saw("/ext-authz/denied"))

print("ext_authz with the body — past max_request_bytes: refused as truncated")
status, _ = upload(15016, "/ext-authz/truncated", [b'{"tool": "', b"x" * 2048, b'"}'])
check("the agent gets a 403", status == 403)
check("nothing reaches the upstream", not recorder.saw("/ext-authz/truncated"))

finish()
