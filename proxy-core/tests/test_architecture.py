"""Executable boundaries between the workspace members.

Every member may import its own `proxy.<name>` package and the members its
`pyproject.toml` depends on, and no other. One virtual environment holds every
member, so an undeclared import works in the suite and in the image of a member
that happens to install the other one, and fails in the image of one that does
not. For proxy-core, which depends on no member, this is the rule that the
core never imports the standalone host or any other interface.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEMBERS = sorted(p.parent for p in ROOT.glob("proxy-*/pyproject.toml"))


def _package(distribution: str) -> str:
    """`proxy-core` -> `proxy.core`, the convention every member follows."""
    return "proxy." + distribution.removeprefix("proxy-").replace("-", "_")


def _allowed(member: Path) -> set[str]:
    project = tomllib.loads((member / "pyproject.toml").read_text())["project"]
    declared = {
        dep.split("[")[0].split("=")[0].split(" ")[0].strip()
        for dep in project.get("dependencies", [])
    }
    return {_package(project["name"])} | {
        _package(d) for d in declared if d.startswith("proxy-")
    }


def test_every_member_is_found():
    # Without this, a glob that stopped matching would leave the test below
    # with nothing to check, and it would pass.
    assert {m.name for m in MEMBERS} >= {
        "proxy-core",
        "proxy-standalone",
        "proxy-envoy-grpc",
    }


@pytest.mark.parametrize("member", MEMBERS, ids=lambda m: m.name)
def test_a_member_imports_only_the_members_it_declares(member: Path):
    allowed = _allowed(member)
    sources = sorted((member / "src").rglob("*.py"))
    assert sources, f"{member.name} has no sources under src/"

    violations: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported = [node.module]
            else:
                continue

            for module in imported:
                parts = module.split(".")
                if parts[0] != "proxy" or len(parts) < 2:
                    continue
                if ".".join(parts[:2]) not in allowed:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} imports {module}"
                    )

    assert not violations, (
        f"{member.name} imports a member it does not declare:\n" + "\n".join(violations)
    )
