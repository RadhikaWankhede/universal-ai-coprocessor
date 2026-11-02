"""One real goal, end to end, with the LIVE planner. Verification, not a test.

    py scripts/live_e2e.py

Builds a throwaway sandbox (two files to move, a git repo with a staged
change), asks GeminiPlanner for a plan, runs it through the real loop - broker,
journal, plugins - with a scripted approval, then undoes it by journal replay.
Prints the plan, each broker verdict, the journal, the on-disk effect, and the
undo. Finally it proves the API key appears nowhere in the journal.

The key is read from UAIC_GEMINI_API_KEY and never printed. Everything happens
in a temp directory that is removed at the end.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from uaic.core import GeminiPlanner, SessionMemory  # noqa: E402
from uaic.core.llm import api_key  # noqa: E402
from uaic.journal import ActionJournal  # noqa: E402
from uaic.journal.undo import UndoEngine  # noqa: E402
from uaic.plugins import DocumentsPlugin, FileSystemPlugin, GitPlugin, TerminalPlugin  # noqa: E402
from uaic.runtime import AgentLoop, Executor  # noqa: E402

GIT = shutil.which("git")


def git(repo: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


def build_sandbox(root: Path) -> None:
    (root / "downloads").mkdir()
    (root / "documents").mkdir()
    (root / "downloads" / "report.pdf").write_text("a report\n", encoding="utf-8")
    (root / "downloads" / "notes.txt").write_text("some notes\n", encoding="utf-8")
    proj = root / "proj"
    proj.mkdir()
    git(proj, "init", "-q")
    git(proj, "config", "user.email", "e2e@example.invalid")
    git(proj, "config", "user.name", "Live E2E")
    (proj / "a.txt").write_text("one\n", encoding="utf-8")
    git(proj, "add", "a.txt")
    git(proj, "commit", "-q", "-m", "initial")
    (proj / "b.txt").write_text("two\n", encoding="utf-8")   # staged, ready to commit
    git(proj, "add", "b.txt")


def dispatcher(plugins: dict):
    return lambda call: plugins[call.tool.split(".", 1)[0]](call)


def main() -> int:
    if GIT is None:
        print("git is not installed; cannot run the end-to-end check")
        return 1
    key = api_key("UAIC_GEMINI_API_KEY")
    sandbox = Path(tempfile.mkdtemp(prefix="uaic-live-"))
    try:
        build_sandbox(sandbox)
        s = sandbox.as_posix()
        goal = (f"In my workspace at {s}, move the file downloads/report.pdf to "
                f"documents/report.pdf and move downloads/notes.txt to "
                f"documents/notes.txt, then commit the git repository at {s}/proj "
                f"with the message 'tidy downloads'.")

        journal = ActionJournal(sandbox / "journal.db")
        plugins = {"fs": FileSystemPlugin(sandbox / "_trash").execute,
                   "git": GitPlugin().execute,
                   "doc": DocumentsPlugin().execute,
                   "terminal": TerminalPlugin().execute}
        asked: list = []

        loop = AgentLoop(
            GeminiPlanner(timeout=120),
            lambda confirm: Executor(journal, plugins, confirm=confirm),
            approve=lambda plan: [s],                       # grant the whole sandbox
            confirm=lambda call, verdict: (asked.append(call.tool), True)[1],
            memory=SessionMemory())

        print(f"model: {loop._planner._model}")
        print(f"goal: {goal}\n")
        report = loop.run(goal, "live-e2e")

        print("=== plan the live model proposed ===")
        print(report.plan.preview() if report.plan else "(no plan)")
        print(f"\noutcome: {report.outcome.value}   confirmations asked: {asked}")
        if report.message:
            print(f"message: {report.message}")

        print("\n=== per-step broker verdict and status ===")
        for st in report.steps:
            print(f"  {st.call.tool:12} {st.verdict.decision.value:13} "
                  f"{st.verdict.rev.value}/{st.verdict.risk.value:8} -> {st.status}")

        print("\n=== journal (as the audit trail) ===")
        for row in journal.rows():
            print(f"  seq {row.seq}: {row.tool:12} {row.decision.value:13} {row.status}")

        print("\n=== on-disk effect ===")
        for rel in ("documents/report.pdf", "documents/notes.txt",
                    "downloads/report.pdf", "downloads/notes.txt"):
            print(f"  {'exists ' if (sandbox / rel).exists() else 'absent '} {rel}")
        head = subprocess.run([GIT, "-C", str(sandbox / "proj"), "log", "-1", "--format=%s"],
                              capture_output=True, text=True).stdout.strip()
        print(f"  proj HEAD subject: {head!r}")

        print("\n=== undo by journal replay ===")
        undo = UndoEngine(journal, dispatcher(plugins)).undo_plan("live-e2e")
        print(f"  {undo.describe()}")
        for rel in ("downloads/report.pdf", "downloads/notes.txt"):
            print(f"  {'restored' if (sandbox / rel).exists() else 'MISSING '} {rel}")
        head2 = subprocess.run([GIT, "-C", str(sandbox / "proj"), "log", "-1", "--format=%s"],
                               capture_output=True, text=True).stdout.strip()
        print(f"  proj HEAD subject after undo: {head2!r}")

        print("\n=== key-leak check ===")
        db_bytes = (sandbox / "journal.db").read_bytes()
        leaked = key.encode() in db_bytes
        print(f"  key present in journal.db bytes: {leaked}")
        journal.close()
        return 1 if leaked else 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
