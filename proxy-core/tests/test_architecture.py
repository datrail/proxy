"""Executable boundaries between the workspace members.

Every member may import its own `proxy.<name>` package, the members its
`pyproject.toml` depends on, the standard library, and the third-party
packages it declares, and nothing else. One virtual environment holds every
member, so an undeclared import works in the suite and in the image of a member
that happens to install the other one, and fails in the image of one that does
not. For proxy-core, which depends on no member, this is the rule that the
core never imports the standalone host, any other interface, or what only they
install (gRPC, Envoy's protobufs, FastMCP).
"""

import ast
import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEMBERS = sorted(p.parent for p in ROOT.glob("proxy-*/pyproject.toml"))

# Top-level module -> the distributions that install it.
_PROVIDERS = importlib.metadata.packages_distributions()


def _package(distribution: str) -> str:
    """`proxy-core` -> `proxy.core`, the convention every member follows."""
    return "proxy." + distribution.removeprefix("proxy-").replace("-", "_")


def _normalize(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _requirement_name(requirement: str) -> str:
    """`uvicorn[standard]==0.52.1` -> `uvicorn`."""
    return re.split(r"[\s\[=<>!~;]", requirement, maxsplit=1)[0]


def _declared(member: Path) -> tuple[set[str], set[str]]:
    """The packages of the members `member` may import, and the third-party
    distributions it declares, normalized."""
    project = tomllib.loads((member / "pyproject.toml").read_text())["project"]
    declared = {_requirement_name(d) for d in project.get("dependencies", [])}
    members = {_package(project["name"])} | {
        _package(d) for d in declared if d.startswith("proxy-")
    }
    return members, {_normalize(d) for d in declared if not d.startswith("proxy-")}


def _with_meta_packages(distributions: set[str]) -> set[str]:
    """`distributions`, plus what each one that ships no module of its own
    requires unconditionally: `fastmcp` is only `fastmcp-slim`."""
    shipping = {_normalize(d) for ds in _PROVIDERS.values() for d in ds}
    result = set(distributions)
    for name in distributions - shipping:
        requires = importlib.metadata.distribution(name).requires or []
        result |= {_normalize(_requirement_name(r)) for r in requires if ";" not in r}
    return result


def _imports(path: Path) -> list[tuple[int, str]]:
    """Every absolute import in `path`, with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.lineno, node.module))
    return found


def _find_violations(
    paths: list[Path], members: set[str], distributions: set[str]
) -> list[str]:
    allowed = _with_meta_packages(distributions)
    violations = []
    for path in paths:
        for line, module in _imports(path):
            parts = module.split(".")
            if parts[0] == "proxy":
                if len(parts) >= 2 and ".".join(parts[:2]) not in members:
                    violations.append(f"{path}:{line} imports {module}")
            elif parts[0] not in sys.stdlib_module_names:
                providers = {_normalize(d) for d in _PROVIDERS.get(parts[0], [])}
                if not providers & allowed:
                    violations.append(f"{path}:{line} imports {module}")
    return violations


def test_every_member_is_found():
    # Without this, a glob that stopped matching would leave the test below
    # with nothing to check, and it would pass.
    assert {m.name for m in MEMBERS} >= {
        "proxy-core",
        "proxy-standalone",
        "proxy-envoy-grpc",
    }


@pytest.mark.parametrize("member", MEMBERS, ids=lambda m: m.name)
def test_a_member_imports_only_what_it_declares(member: Path):
    sources = sorted((member / "src").rglob("*.py"))
    assert sources, f"{member.name} has no sources under src/"

    violations = _find_violations(sources, *_declared(member))

    assert not violations, (
        f"{member.name} imports what it does not declare:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "source",
    [
        "import grpc",
        "from envoy.service.auth.v3 import external_auth_pb2",
        "from proxy.envoy_grpc.decision import decide",
        "import proxy.standalone.server",
    ],
)
def test_an_undeclared_import_is_caught(tmp_path, source):
    path = tmp_path / "module.py"
    path.write_text(source + "\n")

    violations = _find_violations([path], *_declared(ROOT / "proxy-core"))

    assert len(violations) == 1


def test_the_standard_library_and_declared_imports_pass(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("import os\nimport httpx\nfrom proxy.core import judge\n")

    assert not _find_violations([path], *_declared(ROOT / "proxy-core"))
