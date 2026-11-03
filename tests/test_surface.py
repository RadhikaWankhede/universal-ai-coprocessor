"""The UI integration surface is the whole surface.

PROJECT_CONTEXT.md, section 8: "One documented module that is the entire
surface a UI may import. Nothing else in uaic/ should need importing by UI
code." This file is that sentence, plus the no-network rule, as tests.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "uaic" / "desktop"
NETWORK_MODULES = ("socket", "socketserver", "http", "xmlrpc", "asyncio",
                   "multiprocessing", "wsgiref", "ssl", "selectors", "urllib",
                   "ftplib", "smtplib", "requests", "websockets", "PySide6.QtNetwork",
                   "PySide6.QtWebSockets", "PySide6.QtHttpServer")


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    files = [path] if path.is_file() else list(path.rglob("*.py"))
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def test_the_desktop_ui_imports_uaic_only_through_the_surface():
    bad = {m for m in imports_of(DESKTOP)
           if m.startswith("uaic.") and not m.startswith(("uaic.surface", "uaic.desktop"))}
    assert not bad, f"uaic/desktop may import only uaic.surface from uaic: {sorted(bad)}"


def test_no_ui_layer_opens_a_network_listener():
    for where in (DESKTOP, ROOT / "uaic" / "app", ROOT / "uaic" / "surface.py"):
        bad = {m for m in imports_of(where)
               if any(m == n or m.startswith(n + ".") for n in NETWORK_MODULES)}
        assert not bad, f"{where.name} must not import networking modules: {sorted(bad)}"


def test_the_surface_exports_what_it_documents():
    import uaic.surface as surface
    for name in surface.__all__:
        assert hasattr(surface, name), name
    doc = surface.__doc__
    for word in ("Ready", "Question", "Progress", "Finished", "Undone", "answer",
                 "cancel", "Never auto-approve", "network listener"):
        assert word in doc, word


def test_the_surface_does_not_hand_out_the_machinery():
    """A UI that can reach a plugin, the broker or the journal could bypass them."""
    import uaic.surface as surface
    for forbidden in ("ActionJournal", "Executor", "AgentLoop", "decide",
                      "FileSystemPlugin", "GitPlugin", "TerminalPlugin",
                      "DocumentsPlugin", "GeminiPlanner", "CapabilitySet", "UndoEngine"):
        assert forbidden not in surface.__all__, forbidden
