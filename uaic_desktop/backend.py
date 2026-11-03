"""Desktop composition root: wire existing backend components, do not implement tools."""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from uaic.contracts import Plan
from uaic.core import GeminiPlanner, Planner, SessionMemory
from uaic.journal import ActionJournal
from uaic.plugins import DocumentsPlugin, FileSystemPlugin, GitPlugin, TerminalPlugin
from uaic.runtime import AgentLoop, Confirmer, Executor, deny_everything

Approver = Callable[[Plan], Sequence[str | Path] | None]


@dataclass(slots=True)
class DesktopBackend:
    loop: AgentLoop
    journal: ActionJournal

    def close(self) -> None:
        self.journal.close()


def _assemble_backend(
    approve: Approver,
    confirm: Confirmer,
    planner: Planner,
    data_dir: Path,
) -> DesktopBackend:
    data_dir.mkdir(parents=True, exist_ok=True)
    journal = ActionJournal(data_dir / "actions.sqlite3")
    try:
        plugins = {
            "fs": FileSystemPlugin(data_dir / "trash").execute,
            "doc": DocumentsPlugin().execute,
            "git": GitPlugin().execute,
            "terminal": TerminalPlugin().execute,
        }
        loop = AgentLoop(
            planner,
            lambda asker: Executor(journal, plugins, confirm=asker),
            approve=approve,
            confirm=confirm,
            memory=SessionMemory(),
        )
    except Exception:
        journal.close()
        raise
    return DesktopBackend(loop=loop, journal=journal)


def create_backend(
    approve: Approver,
    confirm: Confirmer = deny_everything,
) -> DesktopBackend:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; cannot create the local action journal")
    data_dir = Path(local_app_data) / "UniversalAICoprocessor"
    return _assemble_backend(approve, confirm, GeminiPlanner(), data_dir)
