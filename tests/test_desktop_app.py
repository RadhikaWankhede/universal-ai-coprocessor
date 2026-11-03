from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from uaic.contracts import Plan, PlanStep
from uaic.runtime import Outcome, deny_everything
from uaic_desktop.backend import _assemble_backend
from uaic_desktop.window import MainWindow, PlanApprovalDialog


@pytest.fixture(scope="session")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


class FixedPlanner:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan

    def propose(self, prompt, plan_id, context=None):
        return self.plan.model_copy(update={"prompt": prompt, "plan_id": plan_id})


def test_plan_dialog_shows_steps_taint_and_unchecked_folder_grants(qapp, tmp_path):
    source = tmp_path / "Downloads" / "report.pdf"
    destination = tmp_path / "Documents" / "report.pdf"
    downloads = source.parent
    documents = destination.parent
    plan = Plan(
        plan_id="plan-1",
        prompt="move the report",
        summary="Move one report.",
        steps=(
            PlanStep(
                seq=1,
                tool="doc.extract_text",
                args={"path": str(source)},
                rationale="Read the report.",
            ),
            PlanStep(
                seq=2,
                tool="fs.move",
                args={"src": str(source), "dst": str(destination)},
            ),
        ),
        requested_paths=(str(downloads), str(documents)),
        tainted=True,
        taint_sources=("file:///C:/Users/test/Downloads/report.pdf#doc.extract_text",),
    )

    dialog = PlanApprovalDialog(plan)
    text = dialog.plan_view.toPlainText()
    labels = [label.text() for label in dialog.findChildren(QLabel)]

    assert "Steps: 2" in text
    assert "1. doc.extract_text" in text
    assert "2. fs.move" in text
    assert str(source) in text
    assert str(destination) in text
    assert str(downloads) in dialog.folder_paths
    assert str(documents) in dialog.folder_paths
    assert all(not check.isChecked() for check in dialog.folder_checks)
    assert any("UNTRUSTED CONTENT" in label and "report.pdf" in label for label in labels)
    assert any("irreversibility is not provided" in label for label in labels)


def test_approval_requires_an_explicit_click_and_grants_only_selected_folders(
    qapp, tmp_path
):
    plan = Plan(
        plan_id="plan-2",
        prompt="move a file",
        steps=(PlanStep(seq=1, tool="fs.move", args={"src": "a", "dst": "b"}),),
        requested_paths=(str(tmp_path / "A"), str(tmp_path / "B")),
    )
    dialog = PlanApprovalDialog(plan)
    dialog.show()
    qapp.processEvents()

    QTest.keyClick(dialog.approve_button, Qt.Key.Key_Return)
    qapp.processEvents()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.isVisible()

    dialog.folder_checks[0].setChecked(True)
    QTest.mouseClick(dialog.approve_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.granted_paths == (str(tmp_path / "A"),)
    dialog.close()


def test_no_folder_plan_still_requires_explicit_approval(qapp):
    plan = Plan(plan_id="plan-3", prompt="check status")
    dialog = PlanApprovalDialog(plan)

    assert dialog.folder_checks == []
    assert dialog.granted_paths is None
    QTest.mouseClick(dialog.approve_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.granted_paths == ()


def test_backend_loop_refuses_steps_outside_selected_folders(tmp_path):
    downloads = tmp_path / "Downloads"
    documents = tmp_path / "Documents"
    downloads.mkdir()
    documents.mkdir()
    source = downloads / "report.txt"
    destination = documents / "report.txt"
    source.write_text("report", encoding="utf-8")
    plan = Plan(
        plan_id="template",
        prompt="move the report",
        steps=(
            PlanStep(
                seq=1,
                tool="fs.move",
                args={"src": str(source), "dst": str(destination)},
            ),
        ),
        requested_paths=(str(downloads), str(documents)),
    )
    backend = _assemble_backend(
        approve=lambda _: (downloads,),
        confirm=deny_everything,
        planner=FixedPlanner(plan),
        data_dir=tmp_path / "app-data",
    )
    try:
        report = backend.loop.run("move the report", "run-1")
    finally:
        backend.close()

    assert report.outcome is Outcome.REFUSED
    assert report.steps[0].verdict.rule_ids[0] == "BRK.OUT_OF_SCOPE"
    assert source.read_text(encoding="utf-8") == "report"
    assert not destination.exists()


def test_window_runs_loop_on_worker_and_returns_folder_selection(qapp, tmp_path):
    downloads = tmp_path / "Downloads"
    documents = tmp_path / "Documents"
    downloads.mkdir()
    documents.mkdir()
    source = downloads / "report.txt"
    destination = documents / "report.txt"
    source.write_text("report", encoding="utf-8")
    plan = Plan(
        plan_id="template",
        prompt="move the report",
        steps=(
            PlanStep(
                seq=1,
                tool="fs.move",
                args={"src": str(source), "dst": str(destination)},
            ),
        ),
        requested_paths=(str(downloads), str(documents)),
    )

    def backend_factory(*, approve, confirm):
        return _assemble_backend(
            approve,
            confirm,
            FixedPlanner(plan),
            tmp_path / "window-app-data",
        )

    window = MainWindow(backend_factory)
    window.request_input.setPlainText("move the report")

    def approve_downloads() -> None:
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, PlanApprovalDialog):
            QTimer.singleShot(10, approve_downloads)
            return
        dialog.folder_checks[0].setChecked(True)
        dialog.approve_button.click()

    QTimer.singleShot(0, approve_downloads)
    window.start_run()
    worker = window._worker
    assert worker is not None
    finished = QEventLoop()
    worker.finished.connect(finished.quit)
    QTimer.singleShot(5000, finished.quit)
    finished.exec()

    assert not worker.isRunning()
    assert "refused" in window.output.toPlainText()
    assert "BRK.OUT_OF_SCOPE" in window.output.toPlainText()
    assert source.read_text(encoding="utf-8") == "report"
    assert not destination.exists()
    window.close()
