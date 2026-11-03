"""Bulk moves and folder creation: fs.move_matching, fs.restore_batch,
fs.make_folder, fs.remove_empty_folder.

"Move all the .jpg files from Downloads into Pictures" used to be impossible:
the planner cannot know the file names before the plan runs, and a plan is
capped at 25 steps. One pattern-scoped step fixes both - and these tests pin
what it must NOT do: reach outside the two scoped folders, move part of a
batch, or let a tampered undo record send files anywhere else.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from uaic.broker import decide
from uaic.contracts import Decision, ToolCall
from uaic.core import PlanBuilder, SessionMemory
from uaic.journal import ActionJournal, Status, UndoEngine
from uaic.plugins import FileSystemPlugin
from uaic.plugins import fs as fsmod
from uaic.runtime import AgentLoop, Executor, Outcome, freeze_plan


class FakePlanner:
    def __init__(self, plan) -> None:
        self._plan = plan

    def propose(self, prompt, plan_id, context=None):
        return self._plan.model_copy(update={"plan_id": plan_id, "prompt": prompt})


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / "Downloads"
    dst = tmp_path / "Pictures" / "Downloads-photos"      # does not exist yet
    src.mkdir()
    for name in ("a.jpg", "B.JPG", "c.png", "notes.txt"):
        (src / name).write_text(name, encoding="utf-8")
    (src / "album").mkdir()
    (src / "album" / "inner.jpg").write_text("inner", encoding="utf-8")
    return tmp_path, src, dst


def call(tool: str, **args) -> ToolCall:
    return ToolCall(call_id="c1", plan_id="p1", tool=tool, args=args)


def move_jpgs(src: Path, dst: Path) -> ToolCall:
    return call("fs.move_matching", folder=str(src), pattern="*.jpg", destination=str(dst))


# ------------------------------------------------------------------ plugin

def test_moves_matching_files_directly_inside_the_folder_only(ws):
    root, src, dst = ws
    r = FileSystemPlugin(root / "trash").execute(move_jpgs(src, dst))

    assert r.ok, r.error
    assert sorted(p.name for p in dst.iterdir()) == ["B.JPG", "a.jpg"]  # case-insensitive
    assert sorted(p.name for p in src.iterdir()) == ["album", "c.png", "notes.txt"]
    assert (src / "album" / "inner.jpg").exists(), "subfolders are not searched"
    assert r.payload[0].startswith("moved 2 file(s)")
    assert r.inverse is not None and r.inverse.tool == "fs.restore_batch"


def test_the_inverse_puts_back_exactly_what_moved(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(move_jpgs(src, dst))
    (dst / "already-there.jpg").write_text("x", encoding="utf-8")  # not ours

    back = fs.execute(r.inverse)

    assert back.ok, back.error
    assert (src / "a.jpg").exists() and (src / "B.JPG").exists()
    assert [p.name for p in dst.iterdir()] == ["already-there.jpg"]
    assert not (src / "already-there.jpg").exists()


def test_a_batch_record_cannot_be_replayed(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(move_jpgs(src, dst))
    assert fs.execute(r.inverse).ok

    again = fs.execute(r.inverse)

    assert not again.ok
    assert "no batch" in again.error


def test_a_clash_in_the_destination_moves_nothing(ws):
    root, src, dst = ws
    dst.mkdir(parents=True)
    (dst / "b.jpg").write_text("theirs", encoding="utf-8")

    r = FileSystemPlugin(root / "trash").execute(move_jpgs(src, dst))

    assert not r.ok
    assert "nothing was moved" in r.error
    assert (src / "a.jpg").exists() and (src / "B.JPG").exists()
    assert (dst / "b.jpg").read_text(encoding="utf-8") == "theirs"


def test_a_failure_part_way_puts_back_what_already_moved(ws, monkeypatch):
    root, src, dst = ws
    real = shutil.move
    calls = []

    def flaky(a, b):
        calls.append(a)
        if len(calls) == 2:
            raise PermissionError("file is in use")
        return real(a, b)

    monkeypatch.setattr(fsmod.shutil, "move", flaky)
    r = FileSystemPlugin(root / "trash").execute(move_jpgs(src, dst))

    assert not r.ok
    assert (src / "a.jpg").exists() and (src / "B.JPG").exists()
    assert list(dst.iterdir()) == []


@pytest.mark.parametrize("pattern", ["..\\*.jpg", "../*.jpg", "sub/*.jpg",
                                     "C:*.jpg", "", "   "])
def test_a_pattern_must_be_a_file_name_not_a_path(ws, pattern):
    root, src, dst = ws
    r = FileSystemPlugin(root / "trash").execute(
        call("fs.move_matching", folder=str(src), pattern=pattern, destination=str(dst)))
    assert not r.ok
    assert sorted(p.name for p in src.iterdir()) == [
        "B.JPG", "a.jpg", "album", "c.png", "notes.txt"]


def test_too_many_matches_is_refused_whole(ws, monkeypatch):
    root, src, dst = ws
    monkeypatch.setattr(fsmod, "BATCH_LIMIT", 1)
    r = FileSystemPlugin(root / "trash").execute(move_jpgs(src, dst))
    assert not r.ok
    assert "limit" in r.error
    assert (src / "a.jpg").exists()


def test_no_match_still_records_an_undoable_batch(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(call("fs.move_matching", folder=str(src), pattern="*.gif",
                        destination=str(dst)))
    assert r.ok and r.payload[0].startswith("moved 0 file(s)")
    assert fs.execute(r.inverse).ok


# ------------------------------------------------------ tampered undo records

def _tamper(fs: FileSystemPlugin, inverse: ToolCall, edit) -> None:
    path = fs.batch_root / f"{inverse.args['batch_id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    edit(record)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_tampered_record_cannot_send_a_file_elsewhere(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(move_jpgs(src, dst))
    elsewhere = root / "Startup"
    elsewhere.mkdir()

    def redirect(record):
        record["moves"][0]["src"] = str(elsewhere / "a.jpg")
    _tamper(fs, r.inverse, redirect)

    back = fs.execute(r.inverse)

    assert not back.ok
    assert not (elsewhere / "a.jpg").exists()
    assert (dst / "a.jpg").exists() and (dst / "B.JPG").exists(), "refused whole"


def test_a_tampered_record_cannot_pull_a_file_from_elsewhere(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(move_jpgs(src, dst))
    secret = root / "Documents" / "a.jpg"
    secret.parent.mkdir()
    secret.write_text("private", encoding="utf-8")

    def steal(record):
        record["moves"][0]["dst"] = str(secret)
    _tamper(fs, r.inverse, steal)

    assert not fs.execute(r.inverse).ok
    assert secret.exists()


def test_a_record_only_restores_into_the_folder_the_broker_scoped(ws):
    root, src, dst = ws
    fs = FileSystemPlugin(root / "trash")
    r = fs.execute(move_jpgs(src, dst))
    other = root / "Other"
    other.mkdir()

    wrong = r.inverse.model_copy(update={"args": {**r.inverse.args, "folder": str(other)}})
    assert not fs.execute(wrong).ok
    assert (dst / "a.jpg").exists()


@pytest.mark.parametrize("batch_id", ["../../journal", "ABCDEF123456", "x" * 12, ""])
def test_a_batch_id_is_never_a_path(ws, batch_id):
    root, src, _ = ws
    r = FileSystemPlugin(root / "trash").execute(
        call("fs.restore_batch", batch_id=batch_id, folder=str(src)))
    assert not r.ok
    assert "not a batch id" in r.error


# ------------------------------------------------------------------ broker

def _caps(tool: str, *grants: Path):
    builder = PlanBuilder("p1", "x").step(tool, path="unused")
    for g in grants:
        builder.needs_path(str(g))
    plan = builder.build()
    return freeze_plan(plan, plan.requested_paths)


def test_the_destination_must_be_inside_a_granted_folder(ws):
    _, src, dst = ws
    caps = _caps("fs.move_matching", src)                 # destination NOT granted
    verdict = decide(move_jpgs(src, dst), caps)
    assert verdict.decision is Decision.DENY
    assert "BRK.OUT_OF_SCOPE" in verdict.rule_ids


def test_the_source_folder_must_be_inside_a_granted_folder(ws):
    _, src, dst = ws
    caps = _caps("fs.move_matching", dst.parent)
    verdict = decide(move_jpgs(src, dst), caps)
    assert verdict.decision is Decision.DENY
    assert "BRK.OUT_OF_SCOPE" in verdict.rule_ids


def test_a_bulk_move_inside_the_grant_is_allowed_without_a_prompt(ws):
    _, src, dst = ws
    caps = _caps("fs.move_matching", src, dst.parent)
    verdict = decide(move_jpgs(src, dst), caps)
    assert verdict.decision is Decision.ALLOW


def test_remove_empty_folder_cannot_climb_above_the_grant(ws):
    root, src, _ = ws
    target = src / "empty"
    target.mkdir()
    caps = _caps("fs.remove_empty_folder", src)
    verdict = decide(call("fs.remove_empty_folder", path=str(target), up_to=str(root)),
                     caps)
    assert verdict.decision is Decision.DENY
    assert "BRK.OUT_OF_SCOPE" in verdict.rule_ids


# ------------------------------------------------------------- end to end

def run_plan(root: Path, plan):
    journal = ActionJournal(root / "j.db")
    fs = FileSystemPlugin(root / "trash")
    loop = AgentLoop(FakePlanner(plan),
                     lambda asker: Executor(journal, {"fs": fs.execute}, confirm=asker),
                     approve=lambda p: p.requested_paths,
                     confirm=lambda c, v: False,     # nothing here may need a prompt
                     memory=SessionMemory())
    return loop.run(plan.prompt, "run1"), journal, fs


def test_seventy_four_photos_move_in_one_step_and_undo_in_one(tmp_path):
    src, dst = tmp_path / "Downloads", tmp_path / "Pictures" / "Downloads-photos"
    src.mkdir()
    (tmp_path / "Pictures").mkdir()
    names = [f"IMG_{i:04d}.jpg" for i in range(74)]
    for n in names:
        (src / n).write_bytes(b"\xff\xd8" + n.encode())
    (src / "keep.pdf").write_text("pdf", encoding="utf-8")
    plan = (PlanBuilder("p", "Move all the .jpg files in Downloads into Downloads-photos")
            .step("fs.move_matching", folder=str(src), pattern="*.jpg",
                  destination=str(dst))
            .needs_path(str(src)).needs_path(str(dst.parent)).build())

    report, journal, fs = run_plan(tmp_path, plan)

    assert report.outcome is Outcome.COMPLETED, report.describe()
    assert sorted(p.name for p in dst.iterdir()) == names
    assert [p.name for p in src.iterdir()] == ["keep.pdf"]
    [row] = journal.rows()
    assert row.status == Status.EXECUTED

    undo = UndoEngine(journal, fs.execute).undo_plan("run1")

    assert undo.complete, undo.describe()
    assert sorted(p.name for p in src.iterdir()) == sorted([*names, "keep.pdf"])
    assert list(dst.iterdir()) == []


def test_make_folder_then_undo_removes_only_what_it_created(tmp_path):
    base = tmp_path / "Pictures"
    base.mkdir()
    target = base / "2026" / "trip"
    plan = (PlanBuilder("p", "make a folder").step("fs.make_folder", path=str(target))
            .needs_path(str(base)).build())

    report, journal, fs = run_plan(tmp_path, plan)

    assert report.outcome is Outcome.COMPLETED, report.describe()
    assert target.is_dir()
    assert UndoEngine(journal, fs.execute).undo_plan("run1").complete
    assert not (base / "2026").exists()
    assert base.is_dir(), "the folder that already existed stays"


def test_undo_of_make_folder_refuses_once_something_is_inside(tmp_path):
    base = tmp_path / "Pictures"
    base.mkdir()
    target = base / "new"
    plan = (PlanBuilder("p", "make a folder").step("fs.make_folder", path=str(target))
            .needs_path(str(base)).build())
    _, journal, fs = run_plan(tmp_path, plan)
    (target / "photo.jpg").write_text("keep me", encoding="utf-8")

    undo = UndoEngine(journal, fs.execute).undo_plan("run1")

    assert not undo.complete
    assert "not empty" in undo.failed[0][1]
    assert (target / "photo.jpg").read_text(encoding="utf-8") == "keep me"


def test_make_folder_refuses_an_existing_folder(tmp_path):
    r = FileSystemPlugin(tmp_path / "trash").execute(
        call("fs.make_folder", path=str(tmp_path)))
    assert not r.ok
    assert "already exists" in r.error


def test_remove_empty_folder_never_deletes_a_file(tmp_path):
    folder = tmp_path / "box"
    folder.mkdir()
    (folder / "f.txt").write_text("x", encoding="utf-8")
    r = FileSystemPlugin(tmp_path / "trash").execute(
        call("fs.remove_empty_folder", path=str(folder), up_to=str(folder)))
    assert not r.ok
    assert (folder / "f.txt").exists()
