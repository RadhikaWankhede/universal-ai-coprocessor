from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from uaic.contracts import Plan
from uaic.runtime import Confirmer, LoopReport, deny_everything
from uaic_desktop.backend import Approver, DesktopBackend


class BackendFactory(Protocol):
    def __call__(self, *, approve: Approver, confirm: Confirmer) -> DesktopBackend: ...


def _single_line(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def format_plan(plan: Plan) -> str:
    lines = [
        f"Goal: {_single_line(plan.prompt)}",
        f"Summary: {_single_line(plan.summary) if plan.summary else '(none provided)'}",
        f"Steps: {len(plan.steps)} — review every listed step before approving.",
        "",
    ]
    for step in plan.steps:
        lines.append(f"{step.seq}. {step.tool}")
        if step.rationale:
            lines.append(f"   Why: {_single_line(step.rationale)}")
        for key, value in step.args.items():
            lines.append(f"   {key}: {_single_line(value)}")
    return "\n".join(lines)


def format_report(report: LoopReport) -> str:
    lines = [report.describe()]
    if report.plan is not None:
        lines.append(f"Plan id: {report.plan.plan_id}")
    for step in report.steps:
        lines.extend((
            "",
            f"Step {step.call.seq}: {step.call.tool} — {step.status}",
            f"Broker decision: {step.verdict.decision.value}",
            f"Risk: {step.verdict.risk.value}; reversibility: {step.verdict.rev.value}",
        ))
        if step.verdict.reasons:
            lines.append(f"Reason: {'; '.join(step.verdict.reasons)}")
        if step.verdict.rule_ids:
            lines.append(f"Reason codes: {', '.join(step.verdict.rule_ids)}")
        if step.result is not None:
            if step.result.error:
                lines.append(f"Error: {step.result.error}")
            if step.result.payload is not None:
                lines.append("Result (plain text):")
                lines.append(str(step.result.payload))
    return "\n".join(lines)


class _ExplicitButton(QPushButton):
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.ignore()
            return
        super().keyPressEvent(event)


class PlanApprovalDialog(QDialog):
    def __init__(self, plan: Plan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review and approve plan")
        self.setModal(True)
        self._granted_paths: tuple[str, ...] | None = None
        self.folder_checks: list[QCheckBox] = []
        self.folder_paths: list[str] = []

        layout = QVBoxLayout(self)
        heading = QLabel(f"Review all {len(plan.steps)} proposed steps")
        heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(heading)

        warning = QLabel(
            "Step irreversibility is not provided by the backend and is not labeled here. "
            "Step-level confirmation is also not implemented yet; any step needing a "
            "separate confirmation will be declined."
        )
        warning.setWordWrap(True)
        warning.setTextFormat(Qt.TextFormat.PlainText)
        warning.setStyleSheet(
            "background: #fff3cd; color: #664d03; padding: 10px; border-radius: 6px;"
        )
        layout.addWidget(warning)

        if plan.tainted:
            sources = plan.sources_read()
            source_lines = [
                f"{_single_line(source.name)} ({_single_line(source.kind)})"
                + (f" — {_single_line(source.path)}" if source.path else "")
                for source in sources
            ]
            source_text = "\n".join(source_lines) if source_lines else "Source details unavailable."
            taint_notice = QLabel(
                "UNTRUSTED CONTENT INFLUENCED THIS PLAN\n" + source_text
            )
            taint_notice.setWordWrap(True)
            taint_notice.setTextFormat(Qt.TextFormat.PlainText)
            taint_notice.setStyleSheet(
                "background: #f8d7da; color: #58151c; padding: 10px; border-radius: 6px;"
            )
            layout.addWidget(taint_notice)

        self.plan_view = QPlainTextEdit()
        self.plan_view.setReadOnly(True)
        self.plan_view.setPlainText(format_plan(plan))
        self.plan_view.setMinimumHeight(260)
        layout.addWidget(self.plan_view, stretch=1)

        folder_heading = QLabel(
            f"{len(plan.requested_paths)} folders requested — grant only the folders you intend:"
        )
        folder_heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(folder_heading)
        folder_list = QWidget()
        folder_layout = QVBoxLayout(folder_list)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        for requested_path in plan.requested_paths:
            row = QFrame()
            row_layout = QHBoxLayout(row)
            check = QCheckBox("Grant")
            check.setChecked(False)
            self.folder_checks.append(check)
            self.folder_paths.append(requested_path)
            path_label = QLabel(_single_line(requested_path))
            path_label.setTextFormat(Qt.TextFormat.PlainText)
            path_label.setWordWrap(True)
            row_layout.addWidget(check)
            row_layout.addWidget(path_label, stretch=1)
            folder_layout.addWidget(row)
        if not plan.requested_paths:
            folder_layout.addWidget(QLabel("This plan requested no folders."))

        folder_scroll = QScrollArea()
        folder_scroll.setWidgetResizable(True)
        folder_scroll.setWidget(folder_list)
        folder_scroll.setMaximumHeight(180)
        layout.addWidget(folder_scroll)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.decline_button = _ExplicitButton("Decline")
        self.approve_button = _ExplicitButton(
            "Approve selected folders" if plan.requested_paths else "Approve plan"
        )
        for button in (self.decline_button, self.approve_button):
            button.setDefault(False)
            button.setAutoDefault(False)
        self.decline_button.clicked.connect(self.reject)
        self.approve_button.clicked.connect(self._approve)
        buttons.addWidget(self.decline_button)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)
        self.resize(900, 760)

    @property
    def granted_paths(self) -> tuple[str, ...] | None:
        return self._granted_paths

    def _approve(self) -> None:
        self._granted_paths = tuple(
            path
            for path, check in zip(self.folder_paths, self.folder_checks, strict=True)
            if check.isChecked()
        )
        self.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.ignore()
            return
        super().keyPressEvent(event)


class LoopWorker(QThread):
    approval_requested = Signal(object)
    report_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, backend_factory: BackendFactory, request: str) -> None:
        super().__init__()
        self._backend_factory = backend_factory
        self._request = request
        self._approval_lock = threading.Lock()
        self._approval_event: threading.Event | None = None
        self._approval_result: Sequence[str | Path] | None = None

    def _approve(self, plan: Plan) -> Sequence[str | Path] | None:
        event = threading.Event()
        with self._approval_lock:
            if self._approval_event is not None:
                raise RuntimeError("the loop requested overlapping plan approvals")
            self._approval_event = event
            self._approval_result = None
        self.approval_requested.emit(plan)
        event.wait()
        with self._approval_lock:
            result = self._approval_result
            self._approval_event = None
            self._approval_result = None
        return result

    def resolve_approval(self, grants: Sequence[str | Path] | None) -> None:
        with self._approval_lock:
            if self._approval_event is None:
                raise RuntimeError("there is no pending plan approval")
            self._approval_result = grants
            self._approval_event.set()

    def run(self) -> None:
        backend: DesktopBackend | None = None
        report: LoopReport | None = None
        error: str | None = None
        try:
            backend = self._backend_factory(
                approve=self._approve,
                confirm=deny_everything,
            )
            report = backend.loop.run(self._request, uuid.uuid4().hex)
        except Exception as exc:
            error = f"Agent run failed: {exc}"
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    close_error = f"Could not close the action journal: {exc}"
                    error = f"{error}; {close_error}" if error else close_error
        if error is not None:
            self.failed.emit(error)
        elif report is not None:
            self.report_ready.emit(report)


class MainWindow(QMainWindow):
    def __init__(self, backend_factory: BackendFactory) -> None:
        super().__init__()
        self.setWindowTitle("Universal AI Coprocessor")
        self.resize(900, 720)
        self._backend_factory = backend_factory
        self._worker: LoopWorker | None = None

        central = QWidget()
        layout = QVBoxLayout(central)
        intro = QLabel("Describe what you want the agent to do.")
        intro.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(intro)

        self.request_input = QPlainTextEdit()
        self.request_input.setPlaceholderText("Type a goal...")
        self.request_input.setMaximumHeight(130)
        layout.addWidget(self.request_input)

        actions = QHBoxLayout()
        self.run_button = QPushButton("Plan and run")
        self.status_label = QLabel("Ready")
        actions.addWidget(self.run_button)
        actions.addWidget(self.status_label, stretch=1)
        layout.addLayout(actions)

        caveat = QLabel(
            "This initial version has no step-level confirmation dialog or cancellation. "
            "Actions requiring a separate confirmation are declined."
        )
        caveat.setWordWrap(True)
        caveat.setTextFormat(Qt.TextFormat.PlainText)
        caveat.setStyleSheet("color: #664d03;")
        layout.addWidget(caveat)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("The plan and run outcome will appear here.")
        layout.addWidget(self.output, stretch=1)
        self.setCentralWidget(central)

        self.run_button.clicked.connect(self.start_run)

    def start_run(self) -> None:
        request = self.request_input.toPlainText().strip()
        if not request:
            self.status_label.setText("Enter a goal before starting.")
            return
        if self._worker is not None:
            return

        self.output.clear()
        self.status_label.setText("Planning…")
        self.run_button.setEnabled(False)
        self.request_input.setReadOnly(True)
        worker = LoopWorker(self._backend_factory, request)
        worker.approval_requested.connect(self._review_plan)
        worker.report_ready.connect(self._show_report)
        worker.failed.connect(self._show_error)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        worker.start()

    def _review_plan(self, plan: Plan) -> None:
        self.status_label.setText("Waiting for plan approval")
        dialog = PlanApprovalDialog(plan, self)
        dialog.exec()
        grants = (
            dialog.granted_paths
            if dialog.result() == QDialog.DialogCode.Accepted
            else None
        )
        worker = self._worker
        if worker is None:
            raise RuntimeError("plan approval arrived without an active run")
        worker.resolve_approval(grants)
        self.status_label.setText("Running" if grants is not None else "Plan declined")

    def _show_report(self, report: LoopReport) -> None:
        self.output.setPlainText(format_report(report))
        self.status_label.setText(report.outcome.value.replace("_", " ").title())

    def _show_error(self, message: str) -> None:
        self.output.setPlainText(message)
        self.status_label.setText("Run failed")

    def _worker_finished(self) -> None:
        self._worker = None
        self.run_button.setEnabled(True)
        self.request_input.setReadOnly(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(
                self,
                "Agent is running",
                "Wait for the current operation to finish before closing the application.",
            )
            event.ignore()
            return
        event.accept()
