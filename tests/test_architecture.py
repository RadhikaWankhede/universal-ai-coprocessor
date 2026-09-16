"""The one architectural invariant: the planner cannot reach a plugin."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(pkg: str) -> set[str]:
    found: set[str] = set()
    for py in (ROOT / "uaic" / pkg).rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_core_cannot_import_plugins():
    offenders = {m for m in _imports("core") if m.startswith("uaic.plugins")}
    assert not offenders, f"uaic/core must not import plugins: {offenders}"


def test_contracts_is_a_leaf():
    offenders = {m for m in _imports("contracts")
                 if m.startswith("uaic.") and not m.startswith("uaic.contracts")}
    assert not offenders, f"uaic/contracts must import nothing else from uaic: {offenders}"


def test_broker_does_not_import_plugins_or_core():
    bad = {m for m in _imports("broker")
           if m.startswith(("uaic.plugins", "uaic.core", "uaic.journal"))}
    assert not bad, f"broker must depend only on contracts: {bad}"
