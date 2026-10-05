"""Shared fixtures: a recording upstream, and the config the proxy reads."""

import pathlib
from typing import Any

import httpx
import pytest

from core_support import _RAIL_ENVIRONMENT
from standalone_support import _client_kwargs, _upstream_handler


@pytest.fixture
def upstream(monkeypatch):
    """Point every upstream the proxy builds at a recorder, and return its log.

    Patched at the module symbol rather than threaded through `build_gateway`
    as a parameter: a seam that exists only for tests is a seam that can be
    wrong in production without any test noticing.
    """
    from proxy.standalone import server as proxy_module

    seen: list[dict[str, Any]] = []
    handler = _upstream_handler(seen)

    def factory(**kwargs):
        # Through the production factory, not around it: that is where
        # `follow_redirects=False` is applied, and a test client built beside it
        # would not carry what the real one does.
        return proxy_module.upstream_client(
            transport=httpx.MockTransport(handler), **_client_kwargs(kwargs)
        )

    original = proxy_module.StreamableHttpTransport

    def patched(*args, **kwargs):
        # Checked, then replaced. Replacing it unconditionally would mask a
        # mount that stopped passing one: the suite would keep injecting a
        # client with the right settings while production built one with
        # fastmcp's — redirects followed, ambient proxies honoured.
        assert kwargs.get("httpx_client_factory") is proxy_module.upstream_client, (
            "the mount is not building its client through `upstream_client`"
        )
        kwargs["httpx_client_factory"] = factory
        return original(*args, **kwargs)

    monkeypatch.setattr(proxy_module, "StreamableHttpTransport", patched)
    return seen


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a config file and point the proxy at it through the environment.

    Only the environment — no attribute is patched. `config_file()` resolves
    RAIL_PROXY_CONFIG_FILE per call, so patching a module attribute instead
    would leave the variable that the image actually sets untested.
    """

    def write(body: str) -> pathlib.Path:
        path = tmp_path / "bridge.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("RAIL_PROXY_CONFIG_FILE", str(path))
        return path

    return write


@pytest.fixture(autouse=True)
def no_rail_center(monkeypatch):
    """A clean slate for every test in the package."""
    for name in _RAIL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


ONE_UPSTREAM = (
    'schema_version: "1.0"\n'
    "mcp:\n  servers:\n    - name: delivery\n      url: http://upstream.invalid/mcp\n"
)


@pytest.fixture
def config(write_config):
    """One upstream, named `delivery`, and a proxy that attaches nothing.

    Nothing is set: `RAIL_PLUGIN_ENABLED` is off by default and `no_rail_center`
    has cleared the three that would contradict it, so a proxy configured only
    far enough to forward is what an empty environment gets. Tests about the
    ticket turn the plugin on themselves.
    """
    return write_config(ONE_UPSTREAM)
