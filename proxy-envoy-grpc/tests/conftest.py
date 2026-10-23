"""Fixtures for the proxy-envoy-grpc suite."""

import tempfile
from pathlib import Path

import pytest

from core_support import _RAIL_ENVIRONMENT


@pytest.fixture(autouse=True)
def no_rail_center(monkeypatch):
    """A clean slate for every test in the package."""
    for name in _RAIL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def socket_path():
    # A unix socket path is limited to about 100 bytes, which pytest's own
    # tmp_path can exceed.
    with tempfile.TemporaryDirectory(prefix="rail-") as directory:
        yield Path(directory) / "ext.sock"
