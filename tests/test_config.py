"""What the proxy does with a configuration it cannot serve."""

from __future__ import annotations

import pytest

from fastmcp_proxy import proxy as proxy_module


def test_a_missing_config_file_stops_startup(tmp_path, monkeypatch):
    """The image bakes RAIL_PROXY_CONFIG_FILE to a path holding no file, so a
    container started without a mounted config lands here. It stops rather than
    serving no upstream, which would answer every tool list with nothing and
    look like an upstream that had gone quiet."""
    monkeypatch.setattr(proxy_module, "CONFIG_FILE", tmp_path / "absent.yaml")

    with pytest.raises(SystemExit) as exit_info:
        proxy_module.load_servers()

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "body",
    [
        "",
        "mcp:\n  servers: []\n",
        "mcp:\n  servers:\n    - name: no-url\n",
        "mcp:\n  servers:\n    - url: http://no-name.invalid/mcp\n",
    ],
    ids=["empty-file", "empty-list", "name-without-url", "url-without-name"],
)
def test_a_config_naming_no_usable_upstream_stops_startup(tmp_path, monkeypatch, body):
    """An entry needs both halves to be mountable: the name is the namespace
    every tool is prefixed with, the url is where the call goes. One without
    the other is dropped, and a file that leaves nothing behind is fatal."""
    path = tmp_path / "bridge.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(proxy_module, "CONFIG_FILE", path)

    with pytest.raises(SystemExit) as exit_info:
        proxy_module.load_servers()

    assert exit_info.value.code == 2


def test_usable_entries_survive_alongside_unusable_ones(tmp_path, monkeypatch):
    path = tmp_path / "bridge.yaml"
    path.write_text(
        "mcp:\n"
        "  servers:\n"
        "    - name: good\n      url: http://upstream.invalid/mcp\n"
        "    - name: half\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(proxy_module, "CONFIG_FILE", path)

    assert [s["name"] for s in proxy_module.load_servers()] == ["good"]
