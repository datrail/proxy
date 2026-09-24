"""Executable boundaries for the core/ and standalone/ layout."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
FORBIDDEN_ROOTS = {"plugins", "standalone"}


def test_core_never_imports_host_or_plugins():
    violations: list[str] = []

    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            else:
                continue

            for module in imported:
                if module.split(".", 1)[0] in FORBIDDEN_ROOTS:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} imports {module}"
                    )

    assert not violations, "core dependency direction reversed:\n" + "\n".join(
        violations
    )
