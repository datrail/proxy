"""Fixtures for the proxy-envoy-grpc suite."""

from __future__ import annotations

import pytest

from core_support import _RAIL_ENVIRONMENT


@pytest.fixture(autouse=True)
def no_rail_center(monkeypatch):
    """A clean slate for every test in the package."""
    for name in _RAIL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
