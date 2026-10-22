"""The request every use case reads, whichever interface built it."""

from proxy.core.request import build_request


def test_x_rail_and_pseudo_headers_are_left_out():
    request = build_request(
        "POST",
        "http",
        "mcp.example.com",
        "/mcp",
        [
            (":authority", "mcp.example.com"),
            (":path", "/mcp"),
            ("x-rail", "the-ticket"),
            ("X-Rail-Status", "not-found"),
            ("x-rail-foo", "forged"),
            ("x-railway", "kept"),
            ("authorization", "Bearer agent-secret"),
        ],
    )

    assert request.headers == (
        ("x-railway", "kept"),
        ("authorization", "Bearer agent-secret"),
    )


def test_repeated_headers_keep_their_order():
    request = build_request(
        "GET", "http", "a", "/", [("accept", "a"), ("x-trace", "1"), ("accept", "b")]
    )

    assert request.headers == (("accept", "a"), ("x-trace", "1"), ("accept", "b"))


def test_the_target_is_kept_as_received():
    """Never parsed, so a target `decide` refuses still builds a request."""
    request = build_request("GET", "http", "[::1", "/a b?q=1#f", [])

    assert (request.authority, request.path) == ("[::1", "/a b?q=1#f")
