"""The main window: ask, watch, review, undo.

Milestones covered here: window, text input, transcript and status line (1);
live per-step execution view (5); result view and undo (6); and the controls
for offline speech in (7) and out (8). The questions themselves are in
dialogs.py.

The window talks to the system only through `uaic.surface`. It drains the
backend's event queue from a timer on the Qt thread; the backend never calls
into Qt. Nothing here decides anything: refusals are displayed exactly as the
backend reports them, and every approval is a dialog the user answers.
"""
from __future__ import annotations

import queue

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uaic.desktop.dialogs import PlanApprovalDialog, StepConfirmDialog, plain
from uaic.desktop.voice import SpeechToText, TextToSpeech, make_speech_to_text
from uaic.surface import (
    CANCELLED_RULE,
    Backend,
    Cleared,
    Detail,
    Failed,
    Finished,
    HistoryCleared,
    LoopReport,
    Outcome,
    PlanDetail,
    PlanSummary,
    Progress,
    Question,
    Ready,
    Status,
    StepEvent,
    Undone,
)

POLL_MS = 50
GOOD, BAD, PENDING = QColor("#d1e7dd"), QColor("#f8d7da"), QColor("#e2e3e5")


def _item(text: str, color: QColor | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)          # table items are plain text
    if color is not None:
        item.setBackground(color)
    return item


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class MainWindow(QMainWindow):
    # Overridable so tests can drive the window without a human.
    approval_dialog = PlanApprovalDialog
    confirm_dialog = StepConfirmDialog

    def __init__(self, backend: Backend, *, stt: SpeechToText | None = None,
                 tts: TextToSpeech | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.stt = stt or make_speech_to_text()
        self.tts = tts or TextToSpeech()
        # Transcripts from a background stop, as (generation, text, error).
        # A forced stop (a question, a submit, closing) bumps the generation,
        # so a transcript that arrives afterwards is thrown away.
        self._heard: queue.Queue[tuple[int, str, str | None]] = queue.Queue()
        self._listen_gen = 0
        self._transcribing = False
        self._busy = True
        self._can_plan = False
        self._closed = False
        self._asking = False
        self._rows: dict[str, int] = {}          # call_id -> execution-table row
        self._outputs: dict[int, str] = {}       # execution-table row -> step output
        self._plans: dict[str, PlanSummary] = {}

        self.setWindowTitle("Universal AI Coprocessor")
        self.resize(1180, 760)
        self._build()
        self._set_busy(True, "Opening session...")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pump)
        self._timer.start(POLL_MS)

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)

        outer.addWidget(plain("What should I do?", bold=True))
        ask = QHBoxLayout()
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText("e.g. move the PDFs in my Downloads folder into "
                                        "Documents\\Reports")
        self.request.setFixedHeight(70)
        ask.addWidget(self.request, 1)
        buttons = QVBoxLayout()
        self.plan_button = QPushButton("Plan  (Ctrl+Enter)")
        self.plan_button.clicked.connect(self.submit)
        self.speak_button = QPushButton("Speak")
        self.speak_button.setCheckable(True)
        self.speak_button.toggled.connect(self._toggle_listening)
        self.cancel_button = QPushButton("Cancel run")
        self.cancel_button.clicked.connect(self.cancel)
        for b in (self.plan_button, self.speak_button, self.cancel_button):
            buttons.addWidget(b)
        ask.addLayout(buttons)
        outer.addLayout(ask)
        QShortcut(QKeySequence("Ctrl+Return"), self.request, activated=self.submit)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QSplitter(Qt.Orientation.Vertical)
        run_box = QWidget()
        run_layout = QVBoxLayout(run_box)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.addWidget(plain("Execution - every step, including refusals", bold=True))
        self.steps = _table(["#", "Tool", "Arguments", "Broker", "Status", "Detail"])
        self.steps.itemSelectionChanged.connect(self._show_selected_output)
        run_layout.addWidget(self.steps, 2)
        self.result = plain("No run yet.")
        run_layout.addWidget(self.result)
        self.output_label = plain("Output of the selected step", bold=True)
        run_layout.addWidget(self.output_label)
        # What a tool returned - a file list, a document's text. Untrusted
        # content: shown verbatim in a plain-text box, never rendered.
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Select a step to see what it returned.")
        run_layout.addWidget(self.output, 3)
        left.addWidget(run_box)

        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(plain("Transcript", bold=True))
        self.log = QPlainTextEdit()                # plain text: never rendered as markup
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        left.addWidget(log_box)
        split.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(plain("History - select a plan to see its steps and outputs",
                                     bold=True))
        self.history = _table(["Started (UTC)", "Request", "Tools", "State"])
        self.history.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.history.horizontalHeader().setStretchLastSection(False)
        self.history.itemSelectionChanged.connect(self._history_selected)
        right_layout.addWidget(self.history)
        history_buttons = QHBoxLayout()
        self.undo_button = QPushButton("Undo selected plan")
        self.undo_button.clicked.connect(self.undo)
        self.clear_history_button = QPushButton("Clear history")
        self.clear_history_button.clicked.connect(self.clear_history)
        history_buttons.addWidget(self.undo_button, 2)
        history_buttons.addWidget(self.clear_history_button, 1)
        right_layout.addLayout(history_buttons)
        self.new_button = QPushButton("New conversation (forget context)")
        self.new_button.clicked.connect(self.new_conversation)
        right_layout.addWidget(self.new_button)
        self.speak_results = QCheckBox("Read results aloud")
        right_layout.addWidget(self.speak_results)
        split.addWidget(right)
        split.setSizes([780, 400])
        outer.addWidget(split, 1)

        self.setCentralWidget(root)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addWidget(self.status, 1)

        ok, why = self.stt.available()
        self.speak_button.setEnabled(ok)
        tip = why
        if ok:
            engine = getattr(self.stt, "engine", "")
            tip = ("Push to talk: click, speak, click again. "
                   "The words go into the box for you to check.")
            if engine:
                tip += f"  (engine: {engine})"
        self.speak_button.setToolTip(tip)
        if ok and hasattr(self.stt, "warm_up"):
            self.stt.warm_up()             # load the model now, not on the first click
        ok, why = self.tts.available()
        self.speak_results.setEnabled(ok)
        self.speak_results.setToolTip("" if ok else why)

    # ------------------------------------------------------------ actions

    def submit(self) -> None:
        text = self.request.toPlainText().strip()
        if not text or self._busy or not self._can_plan:
            return
        self._stop_listening()
        self.request.clear()
        self._say(f"\nYou: {text}")
        self.steps.setRowCount(0)
        self._rows.clear()
        self._outputs.clear()
        self.output.clear()
        self.result.setText("Planning...")
        self._set_busy(True, "Planning... (nothing runs until you approve)")
        self.backend.run(text)

    def cancel(self) -> None:
        if self._busy:
            self.backend.cancel()
            self.status.setText("Cancelling at the next safe point "
                                "(a step already running will finish)...")

    def undo(self) -> None:
        plan_id = self._selected()
        if plan_id is None or self._busy:
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Undo plan",
                          f"Reverse every undoable step of {plan_id}?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                          self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, f"Undoing {plan_id}...")
        self.backend.undo(plan_id)

    def new_conversation(self) -> None:
        if not self._busy:
            self._set_busy(True, "Starting a new conversation...")
            self.backend.new_conversation()

    def closeEvent(self, event) -> None:
        self._closed = True
        self._timer.stop()
        self._stop_listening()
        self.backend.shutdown()          # pending questions resolve to "decline"
        self.tts.close()
        super().closeEvent(event)

    # ------------------------------------------------------------- speech

    def _toggle_listening(self, on: bool) -> None:
        if on:
            try:
                self.stt.start()
            except Exception as exc:
                self.speak_button.setChecked(False)
                self._say(f"Microphone error: {exc}")
                return
            self.speak_button.setText("Stop - put words in the box")
            self.status.setText("Listening...")
        elif self.stt.listening and hasattr(self.stt, "stop_in_background"):
            # Slow engine: close the mic now, transcribe off the UI thread.
            gen = self._listen_gen
            self._reset_speak_button()
            self._transcribing = True
            self.speak_button.setEnabled(False)
            self.speak_button.setText("Transcribing...")
            self.status.setText("Transcribing what you said...")
            try:
                self.stt.stop_in_background(
                    lambda text, error: self._heard.put((gen, text, error)))
            except Exception as exc:
                self._heard.put((gen, "", f"{type(exc).__name__}: {exc}"))
        else:
            self._put_in_box(self._stop_listening(forced=False))

    def _put_in_box(self, heard: str) -> None:
        if heard:
            # Into the box for the user to read and edit - never submitted.
            current = self.request.toPlainText().strip()
            self.request.setPlainText(f"{current} {heard}".strip())
            self.status.setText("Check the words in the box, then press Plan.")

    def _take_transcripts(self) -> None:
        try:
            while True:
                gen, text, error = self._heard.get_nowait()
                self._transcribing = False
                self.speak_button.setEnabled(self.stt.available()[0])
                self.speak_button.setText("Speak")
                if gen != self._listen_gen:
                    continue               # stopped by a question or a submit: drop it
                if error:
                    self._say(f"Speech error: {error}")
                    self.status.setText("Speech could not be transcribed.")
                elif text:
                    self._put_in_box(text)
                else:
                    self.status.setText("Nothing was heard - check the microphone "
                                        "(Settings > System > Sound > Input).")
        except queue.Empty:
            pass

    def _reset_speak_button(self) -> None:
        if self.speak_button.isChecked():
            self.speak_button.blockSignals(True)
            self.speak_button.setChecked(False)
            self.speak_button.blockSignals(False)

    def _stop_listening(self, *, forced: bool = True) -> str:
        """Close the microphone. A forced stop (a question appeared, a request
        was submitted, the window is closing) throws the recording away."""
        heard = ""
        if forced:
            self._listen_gen += 1          # any transcript still on its way is dropped
        if self.stt.listening:
            try:
                if forced and hasattr(self.stt, "discard"):
                    self.stt.discard()
                else:
                    heard = self.stt.stop()
            except Exception as exc:
                self._say(f"Microphone error: {exc}")
        if self.speak_button.isChecked():
            self.speak_button.blockSignals(True)
            self.speak_button.setChecked(False)
            self.speak_button.blockSignals(False)
        self.speak_button.setText("Speak")
        return heard

    # ------------------------------------------------------------- events

    def _pump(self) -> None:
        self._take_transcripts()
        if self._asking or self._closed:
            return        # a dialog's nested event loop is running; resume after it
        try:
            while not self._closed and not self._asking:
                self.handle(self.backend.events.get_nowait())
        except queue.Empty:
            pass

    def handle(self, event) -> None:
        if isinstance(event, Ready):
            self._can_plan = event.can_plan
            self._show_history(event.history)
            for row in event.unfinished:
                self._say(f"Interrupted last time (journalled, no outcome): "
                          f"{row.plan_id} step {row.seq} {row.tool}")
            if event.can_plan:
                self._set_busy(False, "Ready. Nothing runs without your approval.")
            else:
                self._say(f"Planning is unavailable: {event.planner_error}\n"
                          "History and undo still work.")
                self._set_busy(False, "Planner unavailable - see the transcript.")
        elif isinstance(event, Question):
            self._ask(event)
        elif isinstance(event, Progress):
            self._show_step(event.event)
        elif isinstance(event, Finished):
            self._show_report(event.report)
            self._show_history(event.history)
            self._set_busy(False, f"Run {event.report.outcome.value}.")
        elif isinstance(event, Undone):
            self._say(f"Undo {event.report.plan_id}: {event.report.describe()}")
            self.result.setText(f"Undo: {event.report.describe()}")
            self._show_history(event.history)
            self._set_busy(False, "Undo complete." if event.report.complete
                           else "Undo stopped early - see the transcript.")
        elif isinstance(event, Detail):
            self._show_detail(event.detail)
        elif isinstance(event, HistoryCleared):
            self._show_history(event.history)
            self.steps.setRowCount(0)
            self._rows.clear()
            self._outputs.clear()
            self.output.clear()
            self.result.setText("History cleared.")
            self._say("--- history cleared: saved requests and outputs deleted; "
                      "the audit journal was kept ---")
            self._set_busy(False, "History cleared.")
        elif isinstance(event, Cleared):
            self._say("--- new conversation: earlier context and taint forgotten ---")
            self._set_busy(False, "Ready.")
        elif isinstance(event, Failed):
            self._say(f"Error ({event.action}): {event.message}")
            self._set_busy(False, "Error - see the transcript.")

    def _ask(self, q: Question) -> None:
        self._stop_listening()               # speech never answers a question
        self._asking = True
        try:
            if q.kind == "approve":
                self.status.setText("Waiting for you to approve the plan...")
                answer = self.approval_dialog(q, self).ask()
                self._say("Plan approved." if answer is not None else "Plan declined.")
            else:
                self.status.setText(f"Waiting for you to confirm {q.call.tool}...")
                answer = self.confirm_dialog(q, self).ask()
                self._say(f"{q.call.tool}: {'allowed' if answer is True else 'declined'}.")
        finally:
            self._asking = False
        if not self._closed:
            self.backend.answer(q.qid, answer)
            self.status.setText("Working...")

    # ------------------------------------------------------------ display

    def _show_step(self, e: StepEvent) -> None:
        row = self._rows.get(e.call_id)
        if row is None:
            row = self.steps.rowCount()
            self.steps.insertRow(row)
            self._rows[e.call_id] = row
        args = ", ".join(f"{k}={v}" for k, v in e.args.items())
        broker = e.decision.value + (" (tainted)" if e.tainted else "")
        if e.phase == "judged":
            status, color, detail = "judged", PENDING, "; ".join(e.reasons)
        else:
            status = "cancelled" if CANCELLED_RULE in e.rule_ids else (e.status or "?")
            color = GOOD if e.status == Status.EXECUTED else BAD
            detail = e.error or "; ".join(e.reasons) or ""
            if e.rule_ids and e.status != Status.EXECUTED:
                detail = f"[{', '.join(e.rule_ids)}] {detail}"
        cells = [f"{e.index}/{e.total}", e.tool, args, broker, status, detail]
        for col, text in enumerate(cells):
            self.steps.setItem(row, col, _item(text, color))
        self.steps.scrollToItem(self.steps.item(row, 0))
        if e.phase == "finished" and e.output is not None:
            self._outputs[row] = e.output
            self._say(f"Output of step {e.index} ({e.tool}):\n{e.output}")
            self.steps.selectRow(row)              # show the newest output
            self._show_selected_output()

    def _show_selected_output(self) -> None:
        rows = {i.row() for i in self.steps.selectedItems()}
        if not rows:
            return
        row = rows.pop()
        tool = self.steps.item(row, 1).text() if self.steps.item(row, 1) else "step"
        self.output_label.setText(f"Output of step {row + 1} ({tool})")
        self.output.setPlainText(self._outputs.get(row, "(this step returned no output)"))

    def _show_report(self, report: LoopReport) -> None:
        if report.plan is not None:
            self._say(report.plan.preview())
        summary = report.describe()
        self._say(f"Result: {summary}")
        self.result.setText(f"Result: {summary}")
        color = "#146c43" if report.outcome is Outcome.COMPLETED else "#b02a37"
        self.result.setStyleSheet(f"color:{color}; font-weight:600;")
        if self.speak_results.isChecked():
            self.tts.say(f"Run {report.outcome.value}. {len(report.executed)} of "
                         f"{len(report.steps)} steps executed.")

    def _show_history(self, plans: list[PlanSummary]) -> None:
        self._plans = {p.plan_id: p for p in plans}
        # Rebuilding the table must not look like the user picking a row.
        self.history.blockSignals(True)
        self.history.setRowCount(0)
        for p in plans:
            row = self.history.rowCount()
            self.history.insertRow(row)
            if p.undone:
                state = f"{p.undone} undone"
            elif p.executed or p.refused:
                state = f"{p.executed} done" + (f", {p.refused} refused" if p.refused else "")
            else:
                state = p.outcome or "-"
            cells = [p.started[:19].replace("T", " "),
                     p.request or "(request not recorded)",
                     ", ".join(dict.fromkeys(p.tools)) or "-", state]
            for col, text in enumerate(cells):
                item = _item(text)
                item.setData(Qt.ItemDataRole.UserRole, p.plan_id)
                item.setToolTip(text)
                self.history.setItem(row, col, item)
        self.history.blockSignals(False)
        self._refresh()

    def _history_selected(self) -> None:
        self._refresh()
        plan_id = self._selected()
        if plan_id is not None and not self._busy:
            self.backend.detail(plan_id)

    def _show_detail(self, d: PlanDetail) -> None:
        """Show a past plan in the execution view: its steps and their outputs."""
        if self._busy or d.plan_id != self._selected():
            return                                  # stale, or a run is on screen
        self.steps.setRowCount(0)
        self._rows.clear()
        self._outputs.clear()
        self.output.clear()
        missing = ("(not recorded - this plan ran before outputs were saved)"
                   if not d.outputs_recorded else "(this step returned no output)")
        for row, s in enumerate(d.steps):
            self.steps.insertRow(row)
            color = GOOD if s.status == Status.EXECUTED else (
                PENDING if s.status == Status.UNDONE else BAD)
            args = ", ".join(f"{k}={v}" for k, v in s.args.items())
            cells = [str(s.seq), s.tool, args, s.decision, s.status, s.error or ""]
            for col, text in enumerate(cells):
                self.steps.setItem(row, col, _item(text, color))
            self._outputs[row] = s.output if s.output is not None else missing
        when = d.started[:19].replace("T", " ")
        asked = d.request or "(request not recorded)"
        self.result.setStyleSheet("color:#555; font-weight:600;")
        self.result.setText(f"From history, {when} UTC - you asked: {asked}"
                            + (f"\nResult: {d.summary}" if d.summary else ""))
        if d.steps:
            self.steps.selectRow(len(d.steps) - 1)
            self._show_selected_output()
        else:
            self.output_label.setText("Output of the selected step")
            self.output.setPlainText("(no steps ran for this plan)")

    def clear_history(self) -> None:
        if self._busy:
            return
        box = QMessageBox(
            QMessageBox.Icon.Warning, "Clear history",
            "Clear the history list and delete every saved request and output?\n\n"
            "The audit journal is kept (it can never be deleted), but plans from "
            "before now will no longer be listed, so they can no longer be undone "
            "from this window.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, "Clearing history...")
        self.backend.clear_history()

    def _selected(self) -> str | None:
        items = self.history.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.status.setText(message)
        self._refresh()

    def _refresh(self) -> None:
        idle = not self._busy
        self.plan_button.setEnabled(idle and self._can_plan)
        self.new_button.setEnabled(idle and self._can_plan)
        self.cancel_button.setEnabled(self._busy and self._can_plan)
        plan = self._plans.get(self._selected() or "")
        self.undo_button.setEnabled(idle and plan is not None and plan.undoable)
        self.clear_history_button.setEnabled(idle and bool(self._plans))
