"""The PySide6 desktop app, driven offscreen.

What is pinned is the security-relevant interaction, not the pixels: every
exit from a question except one button declines; nothing is pre-granted;
untrusted text is never rendered as markup; refusals are shown; speech fills
the request box and never answers anything.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from uaic.app.backend import Question  # noqa: E402
from uaic.core import PlanBuilder  # noqa: E402
from uaic.desktop import dialogs  # noqa: E402
from uaic.desktop.window import MainWindow  # noqa: E402
from uaic.surface import Decision, Rev, Risk, ToolCall, Verdict, describe_plan  # noqa: E402
from uaic.surface import open_backend as real_open_backend  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def approve_question(*paths: str, prompt: str = "tidy", tainted: bool = False) -> Question:
    b = PlanBuilder("p-1", prompt).step("fs.list", path=paths[0] if paths else "C:/x")
    for p in paths:
        b.needs_path(p)
    plan = b.build()
    if tainted:
        plan = plan.model_copy(update={"tainted": True,
                                       "taint_sources": ("file:///C:/evil.pdf#doc",)})
    return Question(qid=1, kind="approve", plan=plan, steps=describe_plan(plan))


def confirm_question() -> Question:
    call = ToolCall(call_id="p-1-1", plan_id="p-1", tool="fs.overwrite", seq=1,
                    args={"path": "C:/x/keep.txt", "content": "<b>new</b>"})
    verdict = Verdict(decision=Decision.CONFIRM_STEP, risk=Risk.HIGH,
                      rev=Rev.IRREVERSIBLE, reasons=("irreversible",),
                      rule_ids=("BRK.GATE",))
    return Question(qid=2, kind="confirm", call=call, verdict=verdict)


def answer_with(dialog, action, delay_ms: int = 50):
    QTimer.singleShot(delay_ms, action)
    return dialog.ask()


def wait_armed(dialog):
    QTest.qWait(dialogs.ARM_DELAY_MS + 100)
    assert dialog.approve_button.isEnabled()


# ------------------------------------------------------------- approval screen

def test_no_requested_folder_is_pre_ticked(app, tmp_path):
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path / "a"), str(tmp_path / "b")))
    assert d.granted() == []
    assert d.folders.count() == 2


def test_approving_grants_exactly_the_ticked_folders(app, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    d = dialogs.PlanApprovalDialog(approve_question(a, b))
    d.folders.item(1).setCheckState(Qt.CheckState.Checked)
    assert answer_with(d, lambda: (wait_armed(d), d.approve_button.click()), 0) == [b]


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape,
                                 Qt.Key.Key_Space])
def test_a_keypress_never_approves(app, tmp_path, key):
    """Enter, Escape and a stray Space (on the focused Decline) all say no -
    even after the approve button is armed and a folder is ticked."""
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path)))
    d.folders.item(0).setCheckState(Qt.CheckState.Checked)
    still_open = []

    def press():
        wait_armed(d)
        QTest.keyClick(d.focusWidget() or d, key)
        if d.isVisible():                       # the key did nothing: record it, close
            still_open.append(True)
            d.reject()
    assert answer_with(d, press, 0) is None
    assert still_open == [], "the key should have closed the dialog by declining"


def test_closing_the_window_declines(app, tmp_path):
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path)))
    d.folders.item(0).setCheckState(Qt.CheckState.Checked)
    assert answer_with(d, d.close) is None


def test_decline_is_default_and_approve_starts_disarmed(app, tmp_path):
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path)))
    assert d.decline_button.isDefault()
    assert not d.approve_button.autoDefault()
    assert not d.approve_button.isEnabled()
    d.approve_button.click()                     # too early: nothing happens
    assert d.result_value is None


def test_untrusted_text_is_never_rendered_as_markup(app, tmp_path):
    hostile = '<a href="https://evil">click</a><b>APPROVED BY ADMIN</b>'
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path), prompt=hostile))
    labels = d.findChildren(QLabel)
    assert labels
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in labels)
    assert any(lbl.text() == hostile for lbl in labels)       # shown literally


def test_each_step_shows_its_manifest_risk_and_undo(app, tmp_path):
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path)))
    row = [d.steps.item(0, c).text() for c in range(d.steps.columnCount())]
    assert row[1] == "fs.list"
    assert row[4] == "low"
    assert row[5] == "changes nothing"


def test_a_tainted_plan_says_where_the_content_came_from(app, tmp_path):
    d = dialogs.PlanApprovalDialog(approve_question(str(tmp_path), tainted=True))
    assert any("evil.pdf" in lbl.text() for lbl in d.findChildren(QLabel))


def test_broad_folders_are_flagged(app):
    d = dialogs.PlanApprovalDialog(approve_question("C:/"))
    assert "whole drive" in d.folders.item(0).text()
    d.folders.item(0).setCheckState(Qt.CheckState.Checked)
    assert d.granted() == ["C:/"]                  # the flag is display only


# ---------------------------------------------------------- step confirmation

def test_confirm_allows_only_through_its_button(app):
    d = dialogs.StepConfirmDialog(confirm_question())
    assert answer_with(d, lambda: (wait_armed(d), d.approve_button.click()), 0) is True


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Escape])
def test_confirm_keypress_declines(app, key):
    d = dialogs.StepConfirmDialog(confirm_question())
    assert answer_with(d, lambda: (wait_armed(d), QTest.keyClick(d.focusWidget() or d, key)),
                       0) is None


def test_confirm_shows_the_exact_arguments_as_plain_text(app):
    d = dialogs.StepConfirmDialog(confirm_question())
    assert "<b>new</b>" in d.args.toPlainText()
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in d.findChildren(QLabel))


# ------------------------------------------------------------------ the window

class FakePlanner:
    def __init__(self, plan):
        self._plan = plan

    def propose(self, prompt, plan_id, context=None):
        return self._plan.model_copy(update={"plan_id": plan_id, "prompt": prompt})


class NoSpeech:
    def __init__(self, heard: str = "", ok: bool = False):
        self.heard, self.ok, self.listening, self.started = heard, ok, False, 0

    def available(self):
        return self.ok, "ready" if self.ok else "no model (test)"

    def start(self):
        self.listening, self.started = True, self.started + 1

    def stop(self):
        self.listening = False
        return self.heard

    def say(self, text):
        self.said = text

    def close(self):
        pass


def pump(app, predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("timed out")


@pytest.fixture
def ws(tmp_path):
    src, dst = tmp_path / "Downloads", tmp_path / "Documents"
    src.mkdir()
    dst.mkdir()
    (src / "a.pdf").write_text("x", encoding="utf-8")
    return tmp_path, src, dst


def window_for(tmp_path, plan, *, stt=None, approve=None):
    backend = real_open_backend(tmp_path / "data", planner=FakePlanner(plan))
    w = MainWindow(backend, stt=stt or NoSpeech(), tts=NoSpeech())
    asked = []

    class AutoApproval:
        def __init__(self, q, parent=None):
            asked.append(q)

        def ask(self):
            return approve(asked[-1]) if approve else list(asked[-1].plan.requested_paths)

    w.approval_dialog = AutoApproval
    backend.start()
    return w, backend, asked


def test_the_window_runs_shows_each_step_and_records_history(app, ws):
    root, src, dst = ws
    plan = (PlanBuilder("p", "tidy")
            .step("fs.move", src=str(src / "a.pdf"), dst=str(dst / "a.pdf"))
            .needs_path(str(src)).needs_path(str(dst)).build())
    w, backend, asked = window_for(root, plan)
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("tidy my downloads")
        w.submit()
        pump(app, lambda: not w._busy and w._plans)

        assert len(asked) == 1
        assert asked[0].steps[0].tool == "fs.move"
        assert (dst / "a.pdf").exists()
        assert w.steps.rowCount() == 1
        assert w.steps.item(0, 4).text() == "executed"
        assert "completed" in w.result.text()
        assert w.history.rowCount() == 1

        w.history.selectRow(0)
        assert w.undo_button.isEnabled()
        backend.undo(w._selected())
        pump(app, lambda: (src / "a.pdf").exists() and not w._busy)
    finally:
        w.close()


def test_a_refusal_is_displayed_with_its_reason_code(app, ws):
    root, src, _ = ws
    outside = root / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    plan = (PlanBuilder("p", "read")
            .step("fs.read", path=str(outside))
            .needs_path(str(src)).build())
    w, _, _ = window_for(root, plan)
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("read it")
        w.submit()
        pump(app, lambda: not w._busy and w.steps.rowCount())
        assert w.steps.item(0, 4).text() == "denied"
        assert "BRK.OUT_OF_SCOPE" in w.steps.item(0, 5).text()
    finally:
        w.close()


def test_declining_the_plan_runs_nothing(app, ws):
    root, src, dst = ws
    plan = (PlanBuilder("p", "tidy")
            .step("fs.move", src=str(src / "a.pdf"), dst=str(dst / "a.pdf"))
            .needs_path(str(src)).needs_path(str(dst)).build())
    w, _, _ = window_for(root, plan, approve=lambda q: None)
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("tidy")
        w.submit()
        pump(app, lambda: not w._busy and "declined" in w.result.text())
        assert (src / "a.pdf").exists()
    finally:
        w.close()


def test_speech_fills_the_box_and_never_submits(app, ws):
    root, src, _ = ws
    stt = NoSpeech(heard="move the report", ok=True)
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, asked = window_for(root, plan, stt=stt)
    try:
        pump(app, lambda: not w._busy)
        w.speak_button.setChecked(True)
        assert stt.listening
        w.speak_button.setChecked(False)
        assert w.request.toPlainText() == "move the report"
        app.processEvents()
        assert not w._busy and asked == []         # nothing was sent to the planner
    finally:
        w.close()


def test_a_question_stops_the_microphone(app, ws):
    root, src, _ = ws
    stt = NoSpeech(heard="", ok=True)
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    seen = []
    w, _, _ = window_for(root, plan, stt=stt,
                         approve=lambda q: (seen.append(stt.listening), None)[1])
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("list it")
        w.submit()
        w.speak_button.setChecked(True)            # user starts talking mid-run
        pump(app, lambda: not w._busy)
        assert seen == [False]                     # the mic was off when asked
    finally:
        w.close()


def test_without_a_model_the_speak_button_explains_itself(app, tmp_path):
    plan = PlanBuilder("p", "x").step("fs.list", path="C:/x").build()
    w, _, _ = window_for(tmp_path, plan)
    try:
        assert not w.speak_button.isEnabled()
        assert "no model" in w.speak_button.toolTip()
    finally:
        w.close()


def test_cancel_is_only_offered_while_a_run_is_in_flight(app, tmp_path):
    plan = PlanBuilder("p", "x").step("fs.list", path="C:/x").build()
    w, _, _ = window_for(tmp_path, plan)
    try:
        pump(app, lambda: not w._busy)
        assert not w.cancel_button.isEnabled()
    finally:
        w.close()


def test_the_window_shows_what_a_step_returned(app, ws):
    root, src, _ = ws
    plan = PlanBuilder("p", "list").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, _ = window_for(root, plan)
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("list it")
        w.submit()
        pump(app, lambda: not w._busy and w._plans)
        assert w.output.toPlainText() == "a.pdf"
        assert "Output of step 1 (fs.list)" in w.output_label.text()
        assert "a.pdf" in w.log.toPlainText()
    finally:
        w.close()


def run_list_plan(app, w, folder):
    pump(app, lambda: not w._busy)
    w.request.setPlainText(f"list {folder.name}")
    w.submit()
    pump(app, lambda: not w._busy and w._plans)


def test_selecting_a_past_plan_shows_its_outputs_after_restart(app, ws):
    root, src, _ = ws
    plan = PlanBuilder("p", "list").step("fs.list", path=str(src)).needs_path(str(src)).build()
    first, _, _ = window_for(root, plan)
    run_list_plan(app, first, src)
    first.close()

    w, _, _ = window_for(root, plan)                      # the app started again
    try:
        pump(app, lambda: not w._busy and w._plans)
        assert w.history.item(0, 1).text() == "list Downloads"
        assert w.steps.rowCount() == 0
        w.history.selectRow(0)
        pump(app, lambda: w.steps.rowCount() == 1)
        assert w.steps.item(0, 1).text() == "fs.list"
        assert w.output.toPlainText() == "a.pdf"
        assert "you asked: list Downloads" in w.result.text()
    finally:
        w.close()


def test_clear_history_asks_first_and_no_keeps_everything(app, ws, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    root, src, _ = ws
    plan = PlanBuilder("p", "list").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, _ = window_for(root, plan)
    try:
        run_list_plan(app, w, src)
        assert w.clear_history_button.isEnabled()

        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)
        w.clear_history()
        app.processEvents()
        assert w.history.rowCount() == 1 and not w._busy

        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
        w.clear_history()
        pump(app, lambda: not w._busy)
        assert w.history.rowCount() == 0
        assert not w.clear_history_button.isEnabled()
        assert "history cleared" in w.log.toPlainText()
    finally:
        w.close()


# ------------------------------------------- slow (background) speech engines

class SlowSpeech(NoSpeech):
    """An engine like Whisper: stop_in_background() hands the transcript over
    later, from another thread; discard() drops the recording."""

    def __init__(self, heard: str = "", error: str | None = None):
        super().__init__(heard=heard, ok=True)
        self.error, self.pending, self.discarded = error, [], 0
        self.engine = "whisper"

    def stop_in_background(self, deliver):
        self.listening = False
        self.pending.append(deliver)

    def discard(self):
        self.listening = False
        self.discarded += 1

    def finish(self):
        for deliver in self.pending:
            deliver(self.heard, self.error)
        self.pending.clear()


def test_a_slow_engine_transcribes_without_freezing_the_window(app, ws):
    root, src, _ = ws
    stt = SlowSpeech(heard="move the report")
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, asked = window_for(root, plan, stt=stt)
    try:
        pump(app, lambda: not w._busy)
        w.speak_button.setChecked(True)
        w.speak_button.setChecked(False)

        assert not stt.listening                       # the mic closed at once
        assert w.request.toPlainText() == ""           # nothing yet: still decoding
        assert not w.speak_button.isEnabled()
        assert w.speak_button.text() == "Transcribing..."

        stt.finish()
        pump(app, lambda: w.request.toPlainText() == "move the report")
        assert w.speak_button.isEnabled() and w.speak_button.text() == "Speak"
        assert asked == []                             # still never submitted
        assert "whisper" in w.speak_button.toolTip()
    finally:
        w.close()


def test_a_transcript_arriving_after_a_submit_is_dropped(app, ws):
    root, src, _ = ws
    stt = SlowSpeech(heard="delete everything")
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, _ = window_for(root, plan, stt=stt)
    try:
        pump(app, lambda: not w._busy)
        w.speak_button.setChecked(True)
        w.speak_button.setChecked(False)               # transcription under way
        w.request.setPlainText("list it")
        w.submit()                                     # a forced stop
        stt.finish()                                   # ...and then the words arrive
        pump(app, lambda: not w._busy and w.speak_button.isEnabled())
        assert "delete everything" not in w.request.toPlainText()
    finally:
        w.close()


def test_a_question_discards_a_recording_instead_of_transcribing_it(app, ws):
    root, src, _ = ws
    stt = SlowSpeech(heard="yes")
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, _ = window_for(root, plan, stt=stt,
                         approve=lambda q: (seen.append(stt.listening), None)[1])
    seen = []
    try:
        pump(app, lambda: not w._busy)
        w.request.setPlainText("list it")
        w.submit()
        w.speak_button.setChecked(True)
        pump(app, lambda: not w._busy)
        assert seen == [False]
        assert stt.discarded >= 1 and stt.pending == []   # thrown away, not decoded
    finally:
        w.close()


def test_a_speech_error_is_reported_and_the_button_comes_back(app, ws):
    root, src, _ = ws
    stt = SlowSpeech(error="decoder broke")
    plan = PlanBuilder("p", "x").step("fs.list", path=str(src)).needs_path(str(src)).build()
    w, _, _ = window_for(root, plan, stt=stt)
    try:
        pump(app, lambda: not w._busy)
        w.speak_button.setChecked(True)
        w.speak_button.setChecked(False)
        stt.finish()
        pump(app, lambda: w.speak_button.isEnabled())
        assert "decoder broke" in w.log.toPlainText()
        assert w.request.toPlainText() == ""
    finally:
        w.close()
