"""Reading the headers Envoy sends either callout as a `HeaderMap`."""


def read_header_map(header_map) -> list[tuple[str, str]]:
    """Each header's name and value, in the order Envoy sent them."""
    # Envoy fills `raw_value` instead of `value` when the bytes may not be UTF-8.
    return [
        (h.key, h.raw_value.decode("latin-1") if h.raw_value else h.value)
        for h in header_map.headers
    ]
