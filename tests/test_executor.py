"""The execution loop. Nothing reaches a plugin without passing the broker."""
from __future__ import annotations

import pytest

from uaic.broker import CapabilitySet
from uaic.contracts import Decision, ToolCall, ToolResult, Verdict
from uaic.journal import ActionJournal, Status, UndoEngine
from uaic.plugins import FileSystemPlugin
from uaic.runtime import Executor, deny_everything


@pytest.fixture
def journal(tmp_path):
    with ActionJournal(tmp_path / "j.db") as j:
        yield j


@pytest.fixture
def plugin(tmp_path):
    return FileSystemPlugin(tmp_path / "trash")


def caps_for(sandbox, *tools, max_steps: int = 25) -> CapabilitySet:
    return CapabilitySet(plan_id="p1", tools=frozenset(tools),
                         path_grants=(sandbox["downloads"],), max_steps=max_steps)


def call(seq: int, tool: str, **args) -> ToolCall:
    return ToolCall(call_id=f"c{seq}", plan_id="p1", tool=tool, args=args, seq=seq)


def yes(call: ToolCall, verdict: Verdict) -> bool:
    return True


# ---------- allow path ----------

def test_allowed_call_runs_and_is_journalled(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute})
    out = ex.run_one(call(1, "fs.list", path=str(sandbox["downloads"])), 
                     caps_for(sandbox, "fs.list"))
    assert out.status == Status.EXECUTED
    row = journal.get("c1")
    assert row.status == Status.EXECUTED
    assert row.mutates is False, "fs.list changes nothing"


def test_intent_is_written_before_the_plugin_runs(journal, plugin, sandbox):
    seen: list[str] = []

    def spy(c: ToolCall) -> ToolResult:
        seen.append(journal.get(c.call_id).status)   # what the journal says mid-flight
        return plugin.execute(c)

    ex = Executor(journal, {"fs": spy})
    ex.run_one(call(1, "fs.list", path=str(sandbox["downloads"])),
               caps_for(sandbox, "fs.list"))
    assert seen == [Status.INTENDED], "the row must exist and be open during dispatch"


# ---------- refusal paths ----------

def test_denied_call_never_reaches_the_plugin(journal, sandbox):
    touched: list[str] = []

    def never(c: ToolCall) -> ToolResult:
        touched.append(c.call_id)
        return ToolResult(call_id=c.call_id, ok=True)

    ex = Executor(journal, {"fs": never})
    out = ex.run_one(call(1, "fs.delete", path=str(sandbox["documents"] / "secret.docx")),
                     caps_for(sandbox, "fs.delete"))
    assert out.status == Status.DENIED
    assert touched == [], "a denied call must not be dispatched"
    assert journal.get("c1").status == Status.DENIED


def test_refusals_are_in_the_audit_trail(journal, sandbox):
    """The evaluation harness counts these, so they must be recorded."""
    ex = Executor(journal, {"fs": lambda c: ToolResult(call_id=c.call_id, ok=True)})
    ex.run_one(call(1, "mail.send", to="x@y.z", body="hi"),
               caps_for(sandbox, "fs.list"))
    row = journal.get("c1")
    assert row.status == Status.DENIED
    assert row.decision is Decision.DENY


def test_step_confirmation_is_asked_and_can_be_refused(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute}, confirm=deny_everything)
    target = sandbox["downloads"] / "a.pdf"
    out = ex.run_one(call(1, "fs.overwrite", path=str(target), content="x"),
                     caps_for(sandbox, "fs.overwrite"))
    assert out.status == Status.DECLINED
    assert target.read_text() == "a", "declining must leave the file untouched"
    assert journal.get("c1").status == Status.DECLINED


def test_step_confirmation_accepted_lets_it_run(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute}, confirm=yes)
    target = sandbox["downloads"] / "a.pdf"
    out = ex.run_one(call(1, "fs.overwrite", path=str(target), content="x"),
                     caps_for(sandbox, "fs.overwrite"))
    assert out.status == Status.EXECUTED
    assert target.read_text() == "x"


def test_missing_plugin_is_refused_not_guessed(journal, sandbox):
    ex = Executor(journal, {})          # no plugins registered at all
    out = ex.run_one(call(1, "fs.list", path=str(sandbox["downloads"])),
                     caps_for(sandbox, "fs.list"))
    assert out.status == Status.DENIED


# ---------- plan-level behaviour ----------

def test_plan_confirmation_is_asked_only_once(journal, plugin, sandbox):
    asked: list[str] = []

    def once(c: ToolCall, v: Verdict) -> bool:
        asked.append(c.call_id)
        return True

    repo = str(sandbox["downloads"])
    ex = Executor(journal, {"git": lambda c: ToolResult(call_id=c.call_id, ok=True)},
                  confirm=once)
    calls = [call(i, "git.commit", repo=repo, message=f"m{i}") for i in (1, 2, 3)]
    report = ex.run_plan(calls, caps_for(sandbox, "git.commit"))

    assert len(asked) == 1, "compensable actions ask once for the whole plan"
    assert report.confirmations == 1
    assert len(report.executed) == 3


def test_step_confirmation_is_asked_every_time(journal, plugin, sandbox):
    asked: list[str] = []

    def each(c: ToolCall, v: Verdict) -> bool:
        asked.append(c.call_id)
        return True

    ex = Executor(journal, {"fs": plugin.execute}, confirm=each)
    calls = [call(i, "fs.overwrite", path=str(sandbox["downloads"] / "a.pdf"),
                  content=f"v{i}") for i in (1, 2)]
    ex.run_plan(calls, caps_for(sandbox, "fs.overwrite"))
    assert len(asked) == 2, "irreversible actions ask every single time"


def test_reversible_steps_ask_nothing(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute}, confirm=deny_everything)
    calls = [
        call(1, "fs.list", path=str(sandbox["downloads"])),
        call(2, "fs.move", src=str(sandbox["downloads"] / "a.pdf"),
             dst=str(sandbox["downloads"] / "sub" / "a.pdf")),
    ]
    report = ex.run_plan(calls, caps_for(sandbox, "fs.list", "fs.move"))
    assert report.confirmations == 0, "the whole point of the reversibility ladder"
    assert len(report.executed) == 2


def test_plan_can_stop_on_the_first_refusal(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute}, stop_on_refusal=True)
    calls = [
        call(1, "fs.read", path=str(sandbox["documents"] / "secret.docx")),  # denied
        call(2, "fs.list", path=str(sandbox["downloads"])),
    ]
    report = ex.run_plan(calls, caps_for(sandbox, "fs.read", "fs.list"))
    assert report.stopped_early
    assert len(report.steps) == 1


def test_report_describes_the_run(journal, plugin, sandbox):
    ex = Executor(journal, {"fs": plugin.execute})
    report = ex.run_plan([call(1, "fs.list", path=str(sandbox["downloads"]))],
                         caps_for(sandbox, "fs.list"))
    assert "1/1 step(s) executed" in report.describe()


# ---------- the headline demo, end to end ----------

def test_organise_then_undo_restores_everything(journal, plugin, sandbox):
    """Move files into a folder, then undo, and the tree comes back."""
    dl = sandbox["downloads"]
    (dl / "one.txt").write_text("1")
    (dl / "two.txt").write_text("2")
    target = dl / "sorted"
    target.mkdir()

    ex = Executor(journal, {"fs": plugin.execute})
    calls = [
        call(1, "fs.move", src=str(dl / "one.txt"), dst=str(target / "one.txt")),
        call(2, "fs.move", src=str(dl / "two.txt"), dst=str(target / "two.txt")),
        call(3, "fs.delete", path=str(dl / "a.pdf")),
    ]
    report = ex.run_plan(calls, caps_for(sandbox, "fs.move", "fs.delete"))
    assert len(report.executed) == 3
    assert report.confirmations == 0, "every step was reversible"
    assert not (dl / "one.txt").exists() and not (dl / "a.pdf").exists()

    undo = UndoEngine(journal, plugin.execute).undo_plan("p1")
    assert undo.complete, undo.describe()
    assert (dl / "one.txt").read_text() == "1"
    assert (dl / "two.txt").read_text() == "2"
    assert (dl / "a.pdf").read_text() == "a", "the trashed file came back"
    assert list(target.iterdir()) == []
