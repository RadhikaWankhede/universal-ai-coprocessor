"""Progress callbacks and cooperative cancellation.

The properties pinned here are the restrictions, not the features:
progress can observe but never influence; cancellation is read only at safe
points, never mid-dispatch, never answers a confirmation, and leaves what
already ran journalled and undoable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from uaic.contracts import Decision, Rev, Risk, ToolCall, ToolResult, Verdict
from uaic.core import PlanBuilder, SessionMemory
from uaic.journal import ActionJournal, Status, UndoEngine
from uaic.plugins import FileSystemPlugin
from uaic.runtime import (
    CANCELLED_RULE,
    AgentLoop,
    CancelToken,
    Executor,
    Outcome,
    freeze_plan,
)
from uaic.runtime.progress import make_event


class FakePlanner:
    def __init__(self, plan) -> None:
        self._plan = plan

    def propose(self, prompt, plan_id, context=None):
        return self._plan.model_copy(update={"plan_id": plan_id, "prompt": prompt})


@pytest.fixture
def ws(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for name in ("a.txt", "b.txt"):
        (src / name).write_text(name, encoding="utf-8")
    return tmp_path, src, dst


def two_moves(src: Path, dst: Path, *, outside: Path | None = None):
    b = (PlanBuilder("p", "tidy")
         .step("fs.move", src=str(src / "a.txt"), dst=str(dst / "a.txt"))
         .step("fs.move", src=str(src / "b.txt"), dst=str(dst / "b.txt")))
    if outside is not None:
        b.step("fs.read", path=str(outside))
    return b.needs_path(str(src)).needs_path(str(dst)).build()


def build(tmp_path, plan, *, fs=None, confirm=None):
    journal = ActionJournal(tmp_path / "j.db")
    fs = fs or FileSystemPlugin(tmp_path / "trash")
    loop = AgentLoop(FakePlanner(plan),
                     lambda asker: Executor(journal, {"fs": fs.execute}, confirm=asker),
                     approve=lambda p: p.requested_paths,
                     confirm=confirm or (lambda c, v: True),
                     memory=SessionMemory())
    return loop, journal, fs


# ------------------------------------------------------------------ progress

def test_every_step_is_reported_judged_then_finished(ws):
    root, src, dst = ws
    loop, _, _ = build(root, two_moves(src, dst))
    events = []

    report = loop.run("tidy", "run1", progress=events.append)

    assert report.outcome is Outcome.COMPLETED
    assert [(e.phase, e.index, e.total) for e in events] == [
        ("judged", 1, 2), ("finished", 1, 2), ("judged", 2, 2), ("finished", 2, 2)]
    assert all(e.status == Status.EXECUTED for e in events if e.phase == "finished")


def test_refusals_are_reported_with_their_reason_code(ws):
    root, src, dst = ws
    secret = root / "elsewhere.txt"
    secret.write_text("x", encoding="utf-8")
    loop, _, _ = build(root, two_moves(src, dst, outside=secret))
    events = []

    report = loop.run("tidy", "run1", progress=events.append)

    assert report.outcome is Outcome.REFUSED
    last = events[-1]
    assert last.phase == "finished"
    assert last.status == Status.DENIED
    assert last.tool == "fs.read"
    assert "BRK.OUT_OF_SCOPE" in last.rule_ids


def test_a_failing_progress_sink_changes_nothing(ws):
    root, src, dst = ws
    loop, journal, _ = build(root, two_moves(src, dst))

    def explode(event):
        raise RuntimeError("the UI crashed")

    report = loop.run("tidy", "run1", progress=explode)

    assert report.outcome is Outcome.COMPLETED
    assert (dst / "a.txt").exists()
    assert (dst / "b.txt").exists()
    assert [r.status for r in journal.rows()] == [Status.EXECUTED, Status.EXECUTED]


def test_a_sink_cannot_rewrite_the_call_it_observes(ws):
    root, src, dst = ws
    loop, _, _ = build(root, two_moves(src, dst))
    elsewhere = str(root / "hijacked.txt")

    def tamper(event):
        if event.phase == "judged":
            event.args["dst"] = elsewhere          # a copy: has no effect

    loop.run("tidy", "run1", progress=tamper)

    assert (dst / "a.txt").exists()
    assert not Path(elsewhere).exists()


def test_event_arguments_are_redacted():
    call = ToolCall(call_id="c", plan_id="p", tool="x.y", seq=1,
                    args={"token": "s3cret", "path": "C:/a"})
    v = Verdict(decision=Decision.ALLOW, risk=Risk.LOW, rev=Rev.REVERSIBLE)

    event = make_event("judged", call, v, total=1)

    assert event.args["token"] != "s3cret"
    assert event.args["path"] == "C:/a"
    assert call.args["token"] == "s3cret"          # the call itself is untouched


# -------------------------------------------------------------- cancellation

def test_cancel_before_the_plan_is_shown_asks_nothing_and_runs_nothing(ws):
    root, src, dst = ws
    asked = []
    journal = ActionJournal(root / "j.db")
    fs = FileSystemPlugin(root / "t")
    loop = AgentLoop(FakePlanner(two_moves(src, dst)),
                     lambda a: Executor(journal, {"fs": fs.execute}, confirm=a),
                     approve=lambda p: (asked.append(p), p.requested_paths)[1])
    token = CancelToken()
    token.cancel()

    report = loop.run("tidy", "run1", cancel=token)

    assert report.outcome is Outcome.CANCELLED
    assert asked == []
    assert journal.rows() == []


class CancelDuring:
    """A filesystem plugin that presses cancel while step N is being dispatched."""

    def __init__(self, plugin, token, on_seq: int) -> None:
        self._plugin, self._token, self._seq = plugin, token, on_seq
        self.dispatched: list[int] = []

    def execute(self, call: ToolCall) -> ToolResult:
        self.dispatched.append(call.seq)
        if call.seq == self._seq:
            self._token.cancel()
        return self._plugin.execute(call)       # the dispatch still completes


def test_cancel_mid_dispatch_finishes_that_step_and_stops_before_the_next(ws):
    root, src, dst = ws
    token = CancelToken()
    fs = CancelDuring(FileSystemPlugin(root / "trash"), token, on_seq=1)
    loop, journal, _ = build(root, two_moves(src, dst), fs=fs)
    events = []

    report = loop.run("tidy", "run1", cancel=token, progress=events.append)

    assert report.outcome is Outcome.CANCELLED
    assert fs.dispatched == [1]                          # step 2 never dispatched
    assert (dst / "a.txt").exists()                      # step 1 completed whole
    assert (src / "b.txt").exists()
    assert [r.status for r in journal.rows()] == [Status.EXECUTED]
    assert not any(e.index == 2 for e in events)         # step 2 never even judged

    undo = UndoEngine(journal, fs.execute).undo_plan("run1")
    assert undo.complete                                 # completed steps stay undoable
    assert (src / "a.txt").exists()


def test_cancel_does_not_answer_a_confirmation_and_stops_dispatch_after_it(ws):
    """The user is asked; cancels while the question is open; then says yes.
    The question is still asked and answered by the human - but the safe point
    after the confirmation sees the cancel, so nothing is dispatched."""
    root, _, dst = ws
    target = dst / "keep.txt"
    target.write_text("original", encoding="utf-8")
    plan = (PlanBuilder("p", "overwrite")
            .step("fs.overwrite", path=str(target), content="replaced")
            .needs_path(str(dst)).build())
    token = CancelToken()
    asked = []

    def confirm(call, verdict):
        asked.append(call.tool)
        token.cancel()
        return True

    loop, journal, _ = build(root, plan, confirm=confirm)
    report = loop.run("overwrite", "run1", cancel=token)

    assert asked == ["fs.overwrite"]
    assert report.outcome is Outcome.CANCELLED
    assert target.read_text(encoding="utf-8") == "original"
    [row] = journal.rows()
    assert row.status == Status.DECLINED
    assert journal.unfinished() == []                    # no intent was written


def test_a_cancelled_step_is_journalled_with_its_reason_code(ws):
    root, src, dst = ws
    token = CancelToken()
    journal = ActionJournal(root / "j.db")
    ex = Executor(journal, {"fs": FileSystemPlugin(root / "t").execute})
    plan = two_moves(src, dst)
    caps = freeze_plan(plan, plan.requested_paths)
    token.cancel()

    outcome = ex.run_one(plan.to_calls()[0], caps, cancel=token)

    assert outcome.status == Status.DECLINED
    assert not outcome.ran
    assert outcome.verdict.rule_ids[0] == CANCELLED_RULE
    assert (src / "a.txt").exists()
    assert journal.rows()[0].status == Status.DECLINED


def test_without_progress_or_cancel_nothing_changes(ws):
    root, src, dst = ws
    loop, journal, _ = build(root, two_moves(src, dst))
    assert loop.run("tidy", "run1").outcome is Outcome.COMPLETED
    assert len(journal.rows()) == 2


# ------------------------------------------------------------------ output

def test_a_finished_step_carries_its_output_as_plain_text(ws):
    root, src, _ = ws
    plan = PlanBuilder("p", "list").step("fs.list", path=str(src)).needs_path(str(src)).build()
    loop, _, _ = build(root, plan)
    events = []

    loop.run("list", "run1", progress=events.append)

    judged, finished = events
    assert judged.output is None                     # nothing has run yet
    assert finished.output == "a.txt\nb.txt"


def test_a_refused_step_has_no_output(ws):
    root, src, dst = ws
    secret = root / "elsewhere.txt"
    secret.write_text("top secret", encoding="utf-8")
    loop, _, _ = build(root, two_moves(src, dst, outside=secret))
    events = []

    loop.run("tidy", "run1", progress=events.append)

    assert events[-1].status == Status.DENIED
    assert events[-1].output is None


def test_output_is_capped():
    from uaic.runtime.progress import MAX_OUTPUT_CHARS, display_output
    text = display_output("x" * (MAX_OUTPUT_CHARS + 500))
    assert text.startswith("x" * MAX_OUTPUT_CHARS)
    assert text.endswith("[500 more characters not shown]")
    assert display_output(["a", "b"]) == "a\nb"
    assert display_output({"k": 1}) == '{\n  "k": 1\n}'
    assert display_output(None) is None
