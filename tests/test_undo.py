"""Undo walks the journal backwards and stops - never skips - at a wall."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from uaic.contracts import (
    Decision,
    Origin,
    Rev,
    Risk,
    ToolCall,
    ToolResult,
    Verdict,
)
from uaic.journal import ActionJournal, Status, UndoEngine


@pytest.fixture
def journal(tmp_path):
    with ActionJournal(tmp_path / "j.db") as j:
        yield j


def verdict(rev: Rev) -> Verdict:
    return Verdict(decision=Decision.ALLOW, risk=Risk.LOW, rev=rev)


def record(journal, seq, tool, rev, *, inverse=None, ok=True, args=None):
    call = ToolCall(call_id=f"c{seq}", plan_id="p1", tool=tool,
                    args=args or {"src": "a", "dst": "b"}, seq=seq)
    journal.write_intent(call, verdict(rev))
    journal.write_outcome(ToolResult(call_id=call.call_id, ok=ok, inverse=inverse))
    return call


def inv(seq, tool="fs.move", **args):
    return ToolCall(call_id=f"inv{seq}", plan_id="p1", tool=tool,
                    args=args or {"src": "b", "dst": "a"}, seq=seq)


# ---------- the happy path ----------

def test_undo_walks_in_reverse_order(journal):
    for i in (1, 2, 3):
        record(journal, i, "fs.move", Rev.REVERSIBLE, inverse=inv(i))

    seen: list[str] = []

    def execute(call: ToolCall) -> ToolResult:
        seen.append(call.call_id)
        assert call.origin is Origin.UNDO, "undo calls must be tagged as such"
        return ToolResult(call_id=call.call_id, ok=True)

    report = UndoEngine(journal, execute).undo_plan("p1")
    assert seen == ["inv3", "inv2", "inv1"]
    assert report.undone == ["c3", "c2", "c1"]
    assert report.complete


def test_undone_rows_are_marked_in_the_journal(journal):
    record(journal, 1, "fs.move", Rev.REVERSIBLE, inverse=inv(1))
    UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)).undo_plan("p1")
    assert journal.get("c1").status == Status.UNDONE


def test_undo_of_an_empty_plan_is_a_no_op(journal):
    report = UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)) \
        .undo_plan("nothing-here")
    assert report.undone == []
    assert report.complete


# ---------- walls ----------

def test_undo_stops_at_an_irreversible_step(journal):
    record(journal, 1, "fs.move", Rev.REVERSIBLE, inverse=inv(1))
    record(journal, 2, "mail.send", Rev.IRREVERSIBLE, args={"to": "x@y.z", "body": "h"})
    record(journal, 3, "fs.move", Rev.REVERSIBLE, inverse=inv(3))

    report = UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)) \
        .undo_plan("p1")

    assert report.undone == ["c3"]                 # only the step after the wall
    assert report.stopped_at.tool == "mail.send"
    assert report.stop_reason == "irreversible"
    assert not report.complete
    # the step BEFORE the wall must be left alone, not skipped past
    assert journal.get("c1").status == Status.EXECUTED


def test_undo_stops_when_no_inverse_was_recorded(journal):
    record(journal, 1, "fs.move", Rev.REVERSIBLE, inverse=inv(1))
    record(journal, 2, "fs.move", Rev.REVERSIBLE, inverse=None)

    report = UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)) \
        .undo_plan("p1")
    assert report.undone == []
    assert report.stop_reason == "no inverse was recorded"


def test_undo_stops_when_an_inverse_fails(journal):
    record(journal, 1, "fs.move", Rev.REVERSIBLE, inverse=inv(1))
    record(journal, 2, "fs.move", Rev.REVERSIBLE, inverse=inv(2))

    def execute(call: ToolCall) -> ToolResult:
        if call.call_id == "inv2":
            return ToolResult(call_id=call.call_id, ok=False, error="disk full")
        return ToolResult(call_id=call.call_id, ok=True)

    report = UndoEngine(journal, execute).undo_plan("p1")
    assert report.undone == []
    assert report.failed == [("c2", "disk full")]
    assert not report.complete
    assert journal.get("c1").status == Status.EXECUTED


def test_failed_steps_are_not_undone(journal):
    """A call that never succeeded has nothing to reverse."""
    record(journal, 1, "fs.move", Rev.REVERSIBLE, inverse=inv(1), ok=False)
    report = UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)) \
        .undo_plan("p1")
    assert report.undone == []
    assert report.complete


def test_report_describes_where_it_stopped(journal):
    record(journal, 1, "mail.send", Rev.IRREVERSIBLE, args={"to": "a@b.c", "body": "x"})
    report = UndoEngine(journal, lambda c: ToolResult(call_id=c.call_id, ok=True)) \
        .undo_plan("p1")
    text = report.describe()
    assert "stopped at step 1" in text
    assert "mail.send" in text
    assert "irreversible" in text


# ---------- the property that matters ----------

def _hash_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_apply_then_undo_restores_the_tree_exactly(journal, tmp_path):
    """Move a set of files around, undo, and assert the tree is byte-identical."""
    work = tmp_path / "work"
    work.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (work / name).write_text(name * 3)
    sorted_dir = work / "sorted"
    sorted_dir.mkdir()
    before = _hash_tree(work)

    def do_move(call: ToolCall) -> ToolResult:
        src, dst = Path(call.args["src"]), Path(call.args["dst"])
        src.rename(dst)
        return ToolResult(
            call_id=call.call_id, ok=True,
            inverse=ToolCall(call_id="i" + call.call_id, plan_id=call.plan_id,
                             tool="fs.move", seq=call.seq,
                             args={"src": str(dst), "dst": str(src)}),
        )

    for i, name in enumerate(("a.txt", "b.txt", "c.txt"), start=1):
        call = ToolCall(call_id=f"c{i}", plan_id="p1", tool="fs.move", seq=i,
                        args={"src": str(work / name), "dst": str(sorted_dir / name)})
        journal.write_intent(call, verdict(Rev.REVERSIBLE))
        journal.write_outcome(do_move(call))

    assert _hash_tree(work) != before, "the moves should have changed the tree"

    report = UndoEngine(journal, do_move).undo_plan("p1")
    assert report.complete
    assert _hash_tree(work) == before, "undo must restore the tree exactly"
