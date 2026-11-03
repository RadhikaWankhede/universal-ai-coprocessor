"""The desktop application's backend: session wiring and the question bridge.

No display is needed. The Tk window is a thin layer over `Backend`; what is
tested here is everything that decides whether something happens.
"""
from __future__ import annotations

import ast
import queue
import threading
from pathlib import Path

import pytest

from uaic.app.backend import Backend, Cleared, Failed, Finished, Question, Ready, Undone
from uaic.app.session import Session, new_plan_id
from uaic.contracts import Plan
from uaic.core import LLMError, PlanBuilder, PlannerError
from uaic.journal import Status
from uaic.runtime import Outcome

ROOT = Path(__file__).resolve().parents[1]


class FakePlanner:
    def __init__(self, plan: Plan | None = None, raises: Exception | None = None) -> None:
        self._plan = plan
        self._raises = raises
        self.calls = 0

    def propose(self, prompt, plan_id, context=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._plan.model_copy(update={"plan_id": plan_id, "prompt": prompt})


@pytest.fixture
def workspace(tmp_path):
    downloads, documents = tmp_path / "Downloads", tmp_path / "Documents"
    downloads.mkdir()
    documents.mkdir()
    (downloads / "a.pdf").write_text("one\n", encoding="utf-8")
    return tmp_path, downloads, documents


def move_plan(downloads: Path, documents: Path) -> Plan:
    return (PlanBuilder("p", "tidy")
            .step("fs.move", src=str(downloads / "a.pdf"), dst=str(documents / "a.pdf"))
            .needs_path(str(downloads)).needs_path(str(documents))
            .build())


def overwrite_plan(documents: Path) -> Plan:
    target = documents / "keep.txt"
    target.write_text("original\n", encoding="utf-8")
    return (PlanBuilder("p", "overwrite")
            .step("fs.overwrite", path=str(target), content="replaced\n")
            .needs_path(str(documents))
            .build())


def session(tmp_path, planner, **kw) -> Session:
    return Session(tmp_path / "data", planner=planner, **kw)


# ----------------------------------------------------------------- session

def test_an_approved_plan_runs_is_journalled_and_undoes(workspace):
    root, downloads, documents = workspace
    s = session(root, FakePlanner(move_plan(downloads, documents)),
                approve=lambda plan: plan.requested_paths)

    report = s.run("tidy my downloads")

    assert report.outcome is Outcome.COMPLETED
    assert (documents / "a.pdf").exists()
    [summary] = s.history()
    assert summary.plan_id == report.plan.plan_id
    assert summary.executed == 1 and summary.undoable

    undo = s.undo(summary.plan_id)
    assert undo.complete
    assert (downloads / "a.pdf").exists() and not (documents / "a.pdf").exists()
    assert s.history()[0].undone == 1
    s.close()


def test_without_an_approver_every_plan_is_declined(workspace):
    root, downloads, documents = workspace
    s = session(root, FakePlanner(move_plan(downloads, documents)))

    report = s.run("tidy my downloads")

    assert report.outcome is Outcome.DECLINED
    assert (downloads / "a.pdf").exists()
    s.close()


def test_without_a_confirmer_an_irreversible_step_is_declined(workspace):
    root, _, documents = workspace
    s = session(root, FakePlanner(overwrite_plan(documents)),
                approve=lambda plan: plan.requested_paths)

    report = s.run("overwrite it")

    assert report.outcome is Outcome.DECLINED
    assert (documents / "keep.txt").read_text(encoding="utf-8") == "original\n"
    s.close()


def test_a_missing_api_key_opens_the_session_but_cannot_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("UAIC_GEMINI_API_KEY", raising=False)
    s = Session(tmp_path / "data")        # the real live_planner factory

    assert not s.can_plan
    assert "UAIC_GEMINI_API_KEY" in s.planner_error
    report = s.run("anything")
    assert report.outcome is Outcome.PLANNING_FAILED
    assert s.history() == []              # the journal still works offline
    s.close()


@pytest.mark.parametrize("exc", [PlannerError("model said nothing"),
                                 LLMError("no API key: revoked")])
def test_planner_errors_become_a_report_not_a_crash(tmp_path, exc):
    s = session(tmp_path, FakePlanner(raises=exc))

    report = s.run("anything")

    assert report.outcome is Outcome.PLANNING_FAILED
    assert str(exc) in report.message
    s.close()


def test_an_empty_request_never_reaches_the_planner(tmp_path):
    planner = FakePlanner(raises=AssertionError("should not be called"))
    s = session(tmp_path, planner)

    assert s.run("   ").outcome is Outcome.PLANNING_FAILED
    assert planner.calls == 0
    s.close()


def test_the_app_wires_no_mail_plugin(tmp_path):
    s = session(tmp_path, FakePlanner())
    assert "mail" not in s._plugins
    s.close()


def test_plan_ids_do_not_repeat():
    assert len({new_plan_id() for _ in range(1000)}) == 1000


def test_new_conversation_forgets_memory(workspace):
    root, downloads, documents = workspace
    s = session(root, FakePlanner(move_plan(downloads, documents)),
                approve=lambda plan: plan.requested_paths)
    s.run("tidy my downloads")
    assert s.memory.items

    s.new_conversation()

    assert not s.memory.items
    s.close()


# ----------------------------------------------------------------- backend

def make_backend(root: Path, planner) -> Backend:
    backend = Backend(lambda approve, confirm: Session(
        root / "data", planner=planner, approve=approve, confirm=confirm))
    backend.start()
    return backend


def next_event(backend: Backend, kind=None, timeout: float = 5.0):
    while True:
        try:
            event = backend.events.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(f"no {kind.__name__ if kind else 'event'} within {timeout}s")
        if kind is None or isinstance(event, kind):
            return event


def test_backend_asks_the_ui_and_runs_on_approval(workspace):
    root, downloads, documents = workspace
    backend = make_backend(root, FakePlanner(move_plan(downloads, documents)))
    try:
        ready = next_event(backend, Ready)
        assert ready.can_plan

        backend.run("tidy my downloads")
        question = next_event(backend, Question)
        assert question.kind == "approve"
        assert backend.answer(question.qid, list(question.plan.requested_paths))

        finished = next_event(backend, Finished)
        assert finished.report.outcome is Outcome.COMPLETED
        assert (documents / "a.pdf").exists()

        backend.undo(finished.report.plan.plan_id)
        undone = next_event(backend, Undone)
        assert undone.report.complete
        assert (downloads / "a.pdf").exists()
    finally:
        backend.shutdown()
    assert not backend.alive


def test_backend_confirmation_is_yes_only_for_literal_true(workspace):
    root, _, documents = workspace
    backend = make_backend(root, FakePlanner(overwrite_plan(documents)))
    try:
        next_event(backend, Ready)
        backend.run("overwrite it")
        approve = next_event(backend, Question)
        backend.answer(approve.qid, list(approve.plan.requested_paths))
        confirm = next_event(backend, Question)
        assert confirm.kind == "confirm" and confirm.call.tool == "fs.overwrite"

        backend.answer(confirm.qid, "yes")          # truthy, but not True

        finished = next_event(backend, Finished)
        assert finished.report.outcome is Outcome.DECLINED
        assert (documents / "keep.txt").read_text(encoding="utf-8") == "original\n"
    finally:
        backend.shutdown()


def test_a_question_is_answered_at_most_once(workspace):
    root, downloads, documents = workspace
    backend = make_backend(root, FakePlanner(move_plan(downloads, documents)))
    try:
        next_event(backend, Ready)
        backend.run("tidy")
        question = next_event(backend, Question)

        assert backend.answer(question.qid, None)                    # decline
        assert not backend.answer(question.qid, [str(root)])         # too late

        assert next_event(backend, Finished).report.outcome is Outcome.DECLINED
        assert (downloads / "a.pdf").exists()
    finally:
        backend.shutdown()


def test_an_unknown_question_id_is_ignored(tmp_path):
    backend = make_backend(tmp_path, FakePlanner())
    try:
        next_event(backend, Ready)
        assert not backend.answer(999, True)
    finally:
        backend.shutdown()


def test_closing_the_window_mid_question_declines(workspace):
    root, downloads, documents = workspace
    backend = make_backend(root, FakePlanner(move_plan(downloads, documents)))
    next_event(backend, Ready)
    backend.run("tidy")
    next_event(backend, Question)               # the UI never answers it

    backend.shutdown()

    assert not backend.alive
    assert (downloads / "a.pdf").exists()
    s = Session(root / "data", planner=FakePlanner())
    assert all(row.status != Status.EXECUTED for row in s.journal.rows())
    s.close()


def test_a_session_that_cannot_open_reports_it(tmp_path):
    def broken(approve, confirm):
        raise OSError("disk full")
    backend = Backend(broken)
    backend.start()
    failed = next_event(backend, Failed)
    assert "disk full" in failed.message
    backend.shutdown()


def test_new_conversation_command(tmp_path):
    backend = make_backend(tmp_path, FakePlanner())
    try:
        next_event(backend, Ready)
        backend.new_conversation()
        next_event(backend, Cleared)
    finally:
        backend.shutdown()


def test_the_session_is_only_touched_on_the_backend_thread(workspace):
    """The journal's SQLite connection is bound to the thread that opened it."""
    root, downloads, documents = workspace
    seen: set[str] = set()

    class Recording(FakePlanner):
        def propose(self, *a, **kw):
            seen.add(threading.current_thread().name)
            return super().propose(*a, **kw)

    backend = make_backend(root, Recording(move_plan(downloads, documents)))
    try:
        next_event(backend, Ready)
        backend.run("tidy")
        q = next_event(backend, Question)
        backend.answer(q.qid, list(q.plan.requested_paths))
        assert next_event(backend, Finished).report.outcome is Outcome.COMPLETED
    finally:
        backend.shutdown()
    assert seen == {"uaic-backend"}


# ------------------------------------------------------------- architecture

# The README: "There is deliberately no HTTP server, socket or network
# transport between the UI and the backend." This is that sentence as a test.
NETWORK_MODULES = ("socket", "socketserver", "http", "xmlrpc", "asyncio",
                   "multiprocessing", "wsgiref", "ssl", "selectors",
                   "urllib", "ftplib", "smtplib")


def _imports(pkg_dir: Path) -> set[str]:
    found: set[str] = set()
    for py in pkg_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_the_app_opens_no_network_transport():
    bad = {m for m in _imports(ROOT / "uaic" / "app")
           if m.split(".")[0] in NETWORK_MODULES}
    assert not bad, f"uaic/app must not import networking modules: {bad}"


def test_nothing_below_the_app_imports_it():
    for pkg in ("contracts", "broker", "journal", "plugins", "core", "runtime"):
        bad = {m for m in _imports(ROOT / "uaic" / pkg) if m.startswith("uaic.app")}
        assert not bad, f"uaic/{pkg} must not import the app: {bad}"


# ------------------------------------------------- progress, cancel, step views

def test_the_approval_question_carries_manifest_classification(workspace):
    root, _, documents = workspace
    backend = make_backend(root, FakePlanner(overwrite_plan(documents)))
    try:
        next_event(backend, Ready)
        backend.run("overwrite it")
        q = next_event(backend, Question)
        [view] = q.steps
        assert view.tool == "fs.overwrite"
        assert view.declared
        assert view.rev.value == "irreversible"
        assert "CANNOT" in view.undo
        backend.answer(q.qid, None)
    finally:
        backend.shutdown()


def test_an_undeclared_tool_is_shown_as_such_before_approval():
    from uaic.app.session import describe_plan
    plan = PlanBuilder("p", "x").step("nope.explode", target="C:/").build()
    [view] = describe_plan(plan)
    assert not view.declared
    assert "refused" in view.summary


def test_progress_events_reach_the_ui_including_each_finish(workspace):
    from uaic.app.backend import Progress
    root, downloads, documents = workspace
    backend = make_backend(root, FakePlanner(move_plan(downloads, documents)))
    try:
        next_event(backend, Ready)
        backend.run("tidy")
        q = next_event(backend, Question)
        backend.answer(q.qid, list(q.plan.requested_paths))
        phases = []
        while True:
            event = next_event(backend)
            if isinstance(event, Progress):
                phases.append((event.event.phase, event.event.status))
            if isinstance(event, Finished):
                break
        assert phases == [("judged", None), ("finished", Status.EXECUTED)]
    finally:
        backend.shutdown()


def test_cancel_never_answers_an_open_question(workspace):
    """Cancel while the confirmation is on screen: the question stays open
    until the user answers it, and even a 'yes' then dispatches nothing."""
    root, _, documents = workspace
    backend = make_backend(root, FakePlanner(overwrite_plan(documents)))
    try:
        next_event(backend, Ready)
        backend.run("overwrite it")
        approve = next_event(backend, Question)
        backend.answer(approve.qid, list(approve.plan.requested_paths))
        confirm = next_event(backend, Question)

        backend.cancel()
        assert not confirm._done.wait(0.3)               # still waiting for the human
        assert backend.answer(confirm.qid, True)         # and only the human answers

        finished = next_event(backend, Finished)
        assert finished.report.outcome is Outcome.CANCELLED
        assert (documents / "keep.txt").read_text(encoding="utf-8") == "original\n"
    finally:
        backend.shutdown()


def test_cancel_with_nothing_running_is_harmless(tmp_path):
    backend = make_backend(tmp_path, FakePlanner())
    try:
        next_event(backend, Ready)
        backend.cancel()
        assert backend.alive
    finally:
        backend.shutdown()


# ---------------------------------------------- history: outputs and clearing

def list_plan(folder: Path) -> Plan:
    return PlanBuilder("p", "list").step("fs.list", path=str(folder)) \
        .needs_path(str(folder)).build()


def test_a_past_plans_request_and_outputs_can_be_read_after_restart(workspace):
    root, downloads, _ = workspace
    s = session(root, FakePlanner(list_plan(downloads)),
                approve=lambda plan: plan.requested_paths)
    plan_id = s.run("list my downloads").plan.plan_id
    s.close()

    reopened = session(root, FakePlanner())          # a later app session
    [summary] = reopened.history()
    assert summary.request == "list my downloads"
    assert summary.outcome == "completed"
    detail = reopened.plan_detail(plan_id)
    assert detail.request == "list my downloads"
    assert detail.outputs_recorded
    [step] = detail.steps
    assert step.tool == "fs.list"
    assert step.output == "a.pdf"
    assert "fs.list" in detail.preview
    reopened.close()


def test_a_declined_plan_is_listed_with_its_request(workspace):
    root, downloads, documents = workspace
    s = session(root, FakePlanner(move_plan(downloads, documents)))   # declines
    s.run("tidy my downloads")
    [summary] = s.history()
    assert summary.request == "tidy my downloads"
    assert summary.outcome == "declined"
    assert summary.executed == 0
    s.close()


def test_clear_history_deletes_records_but_never_the_journal(workspace):
    root, downloads, _ = workspace
    s = session(root, FakePlanner(list_plan(downloads)),
                approve=lambda plan: plan.requested_paths)
    old = s.run("list my downloads").plan.plan_id
    journal_rows = len(s.journal.rows())
    assert journal_rows == 1

    s.clear_history()

    assert s.history() == []
    assert len(s.journal.rows()) == journal_rows        # the audit trail survives
    assert s.plan_detail(old).steps[0].output is None    # the output is gone
    assert s.plan_detail(old).request == ""
    s.run("list again")                                  # new plans show up again
    assert [p.request for p in s.history()] == ["list again"]
    s.close()


def test_cleared_text_leaves_the_records_file(workspace):
    root, downloads, _ = workspace
    (downloads / "a.pdf").rename(downloads / "very-distinctive-name.pdf")
    s = session(root, FakePlanner(list_plan(downloads)),
                approve=lambda plan: plan.requested_paths)
    s.run("list my downloads")
    s.clear_history()
    s.close()
    raw = b"".join(p.read_bytes() for p in (root / "data").glob("records.db*"))
    assert b"very-distinctive-name" not in raw
    assert b"list my downloads" not in raw


def test_plans_from_before_records_existed_say_so(workspace):
    root, downloads, _ = workspace
    s = session(root, FakePlanner(list_plan(downloads)),
                approve=lambda plan: plan.requested_paths)
    plan_id = s.run("list").plan.plan_id
    s.records._conn.execute("DELETE FROM plans")          # simulate an older run
    s.records._conn.execute("DELETE FROM outputs")
    detail = s.plan_detail(plan_id)
    assert not detail.outputs_recorded
    assert detail.steps[0].tool == "fs.list"
    assert s.history()[0].request == ""
    s.close()


def test_backend_detail_and_clear_history(workspace):
    from uaic.app.backend import Detail, HistoryCleared
    root, downloads, _ = workspace
    backend = make_backend(root, FakePlanner(list_plan(downloads)))
    try:
        next_event(backend, Ready)
        backend.run("list it")
        q = next_event(backend, Question)
        backend.answer(q.qid, list(q.plan.requested_paths))
        plan_id = next_event(backend, Finished).report.plan.plan_id

        backend.detail(plan_id)
        detail = next_event(backend, Detail).detail
        assert detail.steps[0].output == "a.pdf"

        backend.clear_history()
        assert next_event(backend, HistoryCleared).history == []
    finally:
        backend.shutdown()
