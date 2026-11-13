"""Undo in the app goes through the broker and the journal like any other call."""
from __future__ import annotations

import json
from pathlib import Path

from uaic.app.session import Session
from uaic.broker import CapabilitySet, decide
from uaic.contracts import Origin, PlanStep, ToolCall, ToolResult
from uaic.core import ScriptedPlanner
from uaic.journal import Status
from uaic.plugins import FileSystemPlugin
from uaic.plugins.fs import RECORD_MISMATCH
from uaic.runtime import AgentLoop, Executor, Outcome


def verdict_for(call: ToolCall, root: Path):
    return decide(call, CapabilitySet(plan_id="p", tools=frozenset({call.tool}),
                                      path_grants=(root,)))


def journal_step(session: Session, root: Path, inverse: ToolCall, *,
                 plan_id: str = "p", grants=None) -> None:
    """A finished forward step whose recorded inverse is `inverse`."""
    forward = ToolCall(call_id=f"{plan_id}-1", plan_id=plan_id, tool="fs.copy",
                       args={"src": str(root / "a"), "dst": str(root / "b")}, seq=1)
    session.journal.write_intent(forward, verdict_for(forward, root))
    session.journal.write_outcome(ToolResult(call_id=forward.call_id, ok=True, inverse=inverse))
    if grants is not None:
        session.journal.write_grants(plan_id, grants)


def inverse_call(tool: str, **args) -> ToolCall:
    return ToolCall(call_id="undo-p-1", plan_id="p", tool=tool, args=args, seq=1,
                    origin=Origin.UNDO)


def make_session(tmp_path: Path, **kwargs) -> Session:
    kwargs.setdefault("plugins", {"fs": FileSystemPlugin(tmp_path / "data-trash").execute})
    return Session(tmp_path / "data", planner_factory=None, **kwargs)


def test_an_inverse_outside_the_grant_is_refused(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("keep")
    session = make_session(tmp_path)
    journal_step(session, granted,
                 inverse_call("fs.delete", path=str(secret)), grants=[granted])

    report = session.undo("p")

    assert not report.complete
    assert secret.exists()
    assert "denied" in report.failed[0][1]
    session.close()


def test_an_inverse_is_journalled_and_hidden_from_history(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    target = granted / "b"
    target.write_text("copy")
    session = make_session(tmp_path)
    journal_step(session, granted, inverse_call("fs.delete", path=str(target)), grants=[granted])

    assert session.undo("p").complete

    assert not target.exists()
    rows = session.journal.rows("undo:p")
    assert [(r.tool, r.status) for r in rows] == [("fs.delete", Status.EXECUTED)]
    assert session.journal.get("p-1").status == Status.UNDONE
    assert [s.plan_id for s in session.history()] == ["p"]
    session.close()


def test_a_declined_inverse_does_not_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ran: list[ToolCall] = []

    def git(call: ToolCall) -> ToolResult:
        ran.append(call)
        return ToolResult(call_id=call.call_id, ok=True)

    session = make_session(tmp_path, plugins={"git": git}, confirm=lambda call, verdict: False)
    journal_step(session, repo, inverse_call("git.revert", repo=str(repo), sha="abc1234"),
                 grants=[repo])

    report = session.undo("p")

    assert not report.complete and ran == []
    assert session.journal.get("p-1").status == Status.EXECUTED
    session.close()


def test_a_confirmed_inverse_runs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ran: list[ToolCall] = []

    def git(call: ToolCall) -> ToolResult:
        ran.append(call)
        return ToolResult(call_id=call.call_id, ok=True)

    session = make_session(tmp_path, plugins={"git": git}, confirm=lambda call, verdict: True)
    journal_step(session, repo, inverse_call("git.revert", repo=str(repo), sha="abc1234"),
                 grants=[repo])

    assert session.undo("p").complete
    assert len(ran) == 1
    session.close()


def test_a_plan_from_before_grants_were_recorded_can_still_be_undone(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    target = granted / "b"
    target.write_text("copy")
    session = make_session(tmp_path)
    journal_step(session, granted, inverse_call("fs.delete", path=str(target)))

    assert session.journal.grants("p") is None
    assert session.undo("p").complete
    assert not target.exists()
    session.close()


def test_a_failed_undo_can_be_tried_again(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    target = granted / "b"
    session = make_session(tmp_path)
    journal_step(session, granted, inverse_call("fs.delete", path=str(target)), grants=[granted])

    assert not session.undo("p").complete           # nothing there yet
    target.write_text("copy")
    assert session.undo("p").complete
    session.close()


def test_the_loop_records_the_folders_the_user_granted(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    session = make_session(tmp_path)
    planner = ScriptedPlanner([PlanStep(seq=1, tool="fs.list", args={"path": str(granted)})])
    loop = AgentLoop(
        planner,
        lambda asker: Executor(session.journal, session._plugins, confirm=asker),
        approve=lambda plan: [granted])

    assert loop.run("list it", "p9").outcome is Outcome.COMPLETED
    assert session.journal.grants("p9") == (str(granted),)
    session.close()


def test_an_argument_of_the_wrong_type_is_refused(tmp_path):
    call = ToolCall(call_id="c", plan_id="p", tool="git.commit",
                    args={"repo": str(tmp_path), "message": 5}, seq=0)
    verdict = verdict_for(call, tmp_path)
    assert verdict.decision.value == "deny" and verdict.rule_ids == ("BRK.BAD_ARGS",)


def test_an_optional_argument_may_be_none_and_a_bool_is_not_a_string(tmp_path):
    ok = ToolCall(call_id="c", plan_id="p", tool="git.branch",
                  args={"repo": str(tmp_path), "name": "x", "start_point": None})
    assert verdict_for(ok, tmp_path).decision.value != "deny"
    bad = ToolCall(call_id="c", plan_id="p", tool="git.branch",
                   args={"repo": str(tmp_path), "name": True})
    assert verdict_for(bad, tmp_path).rule_ids == ("BRK.BAD_ARGS",)


# --------------------------------------------------------- bulk moves (fs.restore_batch)

def bulk_move(tmp_path: Path):
    """A real fs.move_matching run by the Session, with only Downloads granted."""
    granted = tmp_path / "Downloads"
    granted.mkdir()
    for name in ("a.jpg", "b.jpg"):
        (granted / name).write_text(name)
    fs = FileSystemPlugin(tmp_path / "data-trash")
    planner = ScriptedPlanner([PlanStep(seq=1, tool="fs.move_matching", args={
        "folder": str(granted), "pattern": "*.jpg", "destination": str(granted / "photos")})])
    session = make_session(tmp_path, plugins={"fs": fs.execute}, planner=planner,
                           approve=lambda plan: [granted])
    report = session.run("move the photos")
    assert report.outcome is Outcome.COMPLETED, report.describe()
    return session, fs, granted, report.plan.plan_id


def test_a_bulk_move_is_undone_through_the_broker(tmp_path):
    session, _, granted, plan_id = bulk_move(tmp_path)

    assert session.undo(plan_id).complete

    assert sorted(p.name for p in granted.glob("*.jpg")) == ["a.jpg", "b.jpg"]
    [row] = session.journal.rows(f"undo:{plan_id}")
    assert (row.tool, row.status) == ("fs.restore_batch", Status.EXECUTED)
    assert len(row.args["moved"]) == 2, "every file the undo touched is in the journal"
    session.close()


def test_an_edited_batch_record_cannot_pull_a_file_into_the_grant(tmp_path):
    session, fs, granted, plan_id = bulk_move(tmp_path)
    secret = tmp_path / "Documents" / "tax.jpg"
    secret.parent.mkdir()
    secret.write_text("private")
    [path] = fs.batch_root.glob("*.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["to_folder"] = str(secret.parent)            # the folder and the entry,
    record["moves"][0]["dst"] = str(secret)             # rewritten to agree
    path.write_text(json.dumps(record), encoding="utf-8")

    report = session.undo(plan_id)

    assert not report.complete
    assert secret.read_text() == "private"
    assert not (granted / "tax.jpg").exists()
    assert sorted(p.name for p in (granted / "photos").iterdir()) == ["a.jpg", "b.jpg"]
    [row] = session.journal.rows(f"undo:{plan_id}")
    assert row.status == Status.FAILED and RECORD_MISMATCH in row.error
    session.close()


def test_a_restore_naming_one_file_outside_the_grant_is_refused(tmp_path):
    granted = tmp_path / "granted"
    (granted / "photos").mkdir(parents=True)
    (granted / "photos" / "a.jpg").write_text("a")
    secret = tmp_path / "secret" / "tax.jpg"
    secret.parent.mkdir()
    secret.write_text("private")
    session = make_session(tmp_path)
    journal_step(session, granted, inverse_call(
        "fs.restore_batch", batch_id="0123456789ab", folder=str(granted),
        source=str(granted / "photos"),
        moved=[str(granted / "photos" / "a.jpg"), str(secret)],
        originals=[str(granted / "a.jpg"), str(granted / "tax.jpg")]), grants=[granted])

    report = session.undo("p")

    assert not report.complete
    assert "outside the granted folders" in report.failed[0][1]
    assert secret.exists() and (granted / "photos" / "a.jpg").exists()
    session.close()
