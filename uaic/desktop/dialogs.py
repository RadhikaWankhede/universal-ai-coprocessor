"""The two questions the system ever asks a human.

PlanApprovalDialog (milestones 2 and 3) is the most important screen in the
system: it is where the user grants the capability set that is then frozen
for the whole run. StepConfirmDialog (milestone 4) asks about one step the
broker will not run without a human.

What these dialogs display comes from the backend - the plan, the per-step
classification read from the manifests, the broker's verdict. They decide
nothing. Their safety properties are about the human interaction:

* Decline is the default button and has the focus: Enter, Escape and closing
  the window all decline. Only a click on (or deliberate Space press of) the
  approving button says yes.
* The approving button is disarmed for ARM_DELAY_MS after the dialog appears,
  so a click aimed at the main window cannot land on it.
* No requested folder is pre-ticked: the folder list came from the planner,
  and granting it is the user's act.
* All text is PLAIN. Qt labels guess "rich text" from content by default,
  which would let a file name like "<b>..." restyle the dialog; every label
  here is forced to Qt.PlainText, and tables and text boxes are plain.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uaic.surface import Question, Rev, broad_folder

ARM_DELAY_MS = 700
WARN = "background:#fff3cd; color:#664d03; padding:8px; border-radius:4px;"
DANGER = QColor("#f8d7da")
CAUTION = QColor("#fff3cd")


def plain(text: str, *, bold: bool = False, wrap: bool = True,
          style: str = "") -> QLabel:
    """A label that can never interpret its text as markup."""
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    css = ("font-weight:600;" if bold else "") + style
    if css:
        label.setStyleSheet(css)
    return label


def args_text(args: dict) -> str:
    """Exact arguments, as JSON: unambiguous about types, quoting and spaces."""
    return json.dumps(args, indent=2, ensure_ascii=False, default=str)


def args_lines(args: dict) -> str:
    """Compact arguments for a table cell: one `name: value` per line. Values
    are shown via json.dumps so quotes, spaces and control characters stay
    visible rather than being silently rendered."""
    return "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}"
                     for k, v in args.items()) or "(none)"


class _Question(QDialog):
    """A modal question whose every exit except one button is 'no'."""

    def __init__(self, title: str, approve_label: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(640)
        self.result_value = None
        self.body = QVBoxLayout(self)

        self.decline_button = QPushButton("Decline")
        self.approve_button = QPushButton(approve_label)
        self.decline_button.setDefault(True)        # Enter declines
        self.decline_button.setAutoDefault(True)
        self.approve_button.setAutoDefault(False)   # Enter never approves
        self.approve_button.setEnabled(False)
        self.decline_button.clicked.connect(self.reject)
        self.approve_button.clicked.connect(self._approve)
        QTimer.singleShot(ARM_DELAY_MS, lambda: self.approve_button.setEnabled(True))

    def finish_layout(self) -> None:
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.decline_button)
        row.addWidget(self.approve_button)
        self.body.addLayout(row)
        self.decline_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def taint_banner(self, tainted: bool, sources: tuple[str, ...]) -> None:
        if not tainted:
            return
        shown = "\n".join(f"  - {s}" for s in sources[:6]) or "  (unknown)"
        self.body.addWidget(plain(
            "This was proposed AFTER content from outside your own words was read. "
            f"Check that it is what you asked for.\nRead from:\n{shown}", style=WARN))

    def _approve(self) -> None:
        raise NotImplementedError

    def ask(self):
        """Show the dialog; return the answer (None means declined)."""
        self.result_value = None
        self.exec()
        return self.result_value

    def reject(self) -> None:
        self.result_value = None
        super().reject()


class PlanApprovalDialog(_Question):
    """Read the plan, see what each step can do, grant folders, approve."""

    def __init__(self, question: Question, parent: QWidget | None = None) -> None:
        super().__init__("Approve this plan?", "Approve plan", parent)
        plan = question.plan
        self.resize(1000, 760)
        self.taint_banner(plan.tainted, plan.taint_sources)

        self.body.addWidget(plain("You asked:", bold=True))
        self.body.addWidget(plain(plan.prompt))
        if plan.summary:
            self.body.addWidget(plain(f"Planner's note: {plan.summary}"))

        self.body.addWidget(plain(f"The planner proposes {len(question.steps)} step(s):",
                                  bold=True))
        self.steps = QTableWidget(len(question.steps), 6)
        self.steps.setHorizontalHeaderLabels(
            ["#", "Tool", "What it does", "Arguments", "Risk", "Undo"])
        self.steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps.setWordWrap(True)
        self.steps.verticalHeader().setVisible(False)
        for row, step in enumerate(question.steps):
            cells = [str(step.seq), step.tool,
                     step.summary + (f"\nwhy: {step.rationale}" if step.rationale else ""),
                     args_lines(step.args),
                     step.risk.value if step.risk else "?",
                     step.undo]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not step.declared or step.rev is Rev.IRREVERSIBLE:
                    item.setBackground(DANGER)
                elif step.rev is Rev.COMPENSABLE:
                    item.setBackground(CAUTION)
                self.steps.setItem(row, col, item)
        header = self.steps.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.steps.setColumnWidth(2, 230)
        self.steps.resizeRowsToContents()
        self.body.addWidget(self.steps, 3)

        self.body.addWidget(plain(
            "Tools this run will be allowed to use (frozen on approval): "
            + (", ".join(sorted(plan.tools())) or "none")))

        self.body.addWidget(plain("Folders this plan may use - tick each one you grant:",
                                  bold=True))
        self.folders = QListWidget()
        self.folders.setMaximumHeight(120)
        for path in plan.requested_paths:
            self._add_folder(path, checked=False)
        self.body.addWidget(self.folders)
        if not plan.requested_paths:
            self.body.addWidget(plain("(the plan named no folders)"))
        add = QPushButton("Add folder...")
        add.setAutoDefault(False)
        add.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addStretch(1)
        self.body.addLayout(row)
        self.finish_layout()

    def _add_folder(self, path: str, *, checked: bool) -> None:
        note = "    (whole drive or home folder)" if broad_folder(path) else ""
        item = QListWidgetItem(path + note)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.folders.addItem(item)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Grant a folder")
        if chosen:
            self._add_folder(chosen, checked=True)     # the user picked it themselves

    def granted(self) -> list[str]:
        return [self.folders.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.folders.count())
                if self.folders.item(i).checkState() == Qt.CheckState.Checked]

    def _approve(self) -> None:
        self.result_value = self.granted()
        self.accept()


class StepConfirmDialog(_Question):
    """One step, exactly as it will run, and why the broker is asking."""

    def __init__(self, question: Question, parent: QWidget | None = None) -> None:
        call, verdict = question.call, question.verdict
        super().__init__(f"Allow {call.tool}?", "Allow this step", parent)
        self.taint_banner(call.tainted, call.taint_sources)
        self.body.addWidget(plain(f"Step {call.seq}: {call.tool}", bold=True))
        undo = {Rev.REVERSIBLE: "yes", Rev.COMPENSABLE: "only compensated",
                Rev.IRREVERSIBLE: "NO"}[verdict.rev]
        self.body.addWidget(plain(f"Risk: {verdict.risk.value}    Can be undone: {undo}"))
        if verdict.reasons:
            self.body.addWidget(plain("Why you are being asked: " + "; ".join(verdict.reasons)))
        if verdict.rule_ids:
            self.body.addWidget(plain("Rule: " + ", ".join(verdict.rule_ids)))
        self.body.addWidget(plain("Exactly what it will do:", bold=True))
        self.args = QPlainTextEdit(args_text(call.args))
        self.args.setReadOnly(True)
        self.body.addWidget(self.args)
        self.finish_layout()

    def _approve(self) -> None:
        self.result_value = True
        self.accept()
