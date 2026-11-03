"""The backend thread: owns the Session, turns blocking questions into events.

The agent loop is synchronous - `approve(plan)` and `confirm(call, verdict)`
block until a human answers. A UI cannot block its own thread, so the loop
runs here, on one worker thread that also owns the journal's SQLite
connection, and every question becomes a `Question` event the UI answers.

This is the same-process boundary the README insists on. There is no port,
socket or pipe: a question can only be answered by code holding a reference
to this object, which is the UI in this process. (tests/test_app.py fails if
this package ever imports a networking server module.)

Fail-safe rules:
* A question nobody answers is never assumed answered "yes". If the backend
  shuts down - the window closed mid-question - every pending question
  resolves to its safe default: decline the plan, decline the step.
* Each question is answered at most once; a second answer is ignored.
* A confirmation is "yes" only if the answer is literally True.
"""
from __future__ import annotations

import itertools
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uaic.app.session import PlanDetail, PlanSummary, Session, StepView, describe_plan
from uaic.contracts import Plan, ToolCall, Verdict
from uaic.journal import JournalRow, UndoReport
from uaic.runtime import CancelToken, LoopReport, StepEvent
from uaic.runtime.loop import Approver, Confirmer

SessionFactory = Callable[[Approver, Confirmer], Session]

_POLL = 0.1   # seconds; how often a waiting question re-checks for shutdown


# ------------------------------------------------------------------ events

@dataclass
class Ready:
    can_plan: bool
    planner_error: str | None
    unfinished: list[JournalRow]
    history: list[PlanSummary]


@dataclass
class Question:
    """A question the loop is blocked on. Answer it with Backend.answer()."""
    qid: int
    kind: str                          # "approve" or "confirm"
    plan: Plan | None = None
    steps: tuple[StepView, ...] = ()   # the plan, classified from the manifests
    call: ToolCall | None = None
    verdict: Verdict | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _answer: Any = field(default=None, repr=False)


@dataclass
class Progress:
    """A step was judged or finished. Observational: answering is impossible."""
    event: StepEvent


@dataclass
class Finished:
    report: LoopReport
    history: list[PlanSummary]


@dataclass
class Undone:
    report: UndoReport
    history: list[PlanSummary]


@dataclass
class Failed:
    action: str
    message: str


@dataclass
class Cleared:
    """A new conversation started: memory and taint are reset."""


@dataclass
class Detail:
    """A past plan, as asked for with Backend.detail(plan_id)."""
    detail: PlanDetail


@dataclass
class HistoryCleared:
    """Stored requests and outputs were deleted; the journal was kept."""
    history: list[PlanSummary]


Event = (Ready | Question | Progress | Finished | Undone | Failed | Cleared
         | Detail | HistoryCleared)


# ----------------------------------------------------------------- backend

class Backend:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory
        self.events: queue.Queue[Event] = queue.Queue()
        self._commands: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._pending: dict[int, Question] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._closed = threading.Event()
        self._token: CancelToken | None = None
        self._thread = threading.Thread(target=self._work, name="uaic-backend",
                                        daemon=True)

    # ------------------------------------------------- called from the UI

    def start(self) -> None:
        self._thread.start()

    def run(self, request: str) -> None:
        self._commands.put(("run", request))

    def undo(self, plan_id: str) -> None:
        self._commands.put(("undo", plan_id))

    def new_conversation(self) -> None:
        self._commands.put(("clear", None))

    def detail(self, plan_id: str) -> None:
        """Ask for a past plan's request, steps and outputs (-> Detail)."""
        self._commands.put(("detail", plan_id))

    def clear_history(self) -> None:
        """Delete stored requests and outputs; hide past plans (-> HistoryCleared).
        The journal - the audit trail - is never cleared."""
        self._commands.put(("clear_history", None))

    def cancel(self) -> None:
        """Ask the current run to stop at its next safe point.

        This never answers a pending question - an open approval or
        confirmation still waits for the user - and it never interrupts a
        dispatch in progress. It can only make less happen.
        """
        with self._lock:
            token = self._token
        if token is not None:
            token.cancel()

    def answer(self, qid: int, value: Any) -> bool:
        """Answer a question. Returns False if it was already answered or gone."""
        with self._lock:
            question = self._pending.pop(qid, None)
        if question is None:
            return False
        question._answer = value
        question._done.set()
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        self._closed.set()
        self.cancel()                        # stop the run at its next safe point
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for question in pending:
            question._answer = None          # the safe default for both kinds
            question._done.set()
        self._commands.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # --------------------------------------------- the loop's callbacks

    def _ask(self, question: Question) -> Any:
        if self._closed.is_set():
            return None
        with self._lock:
            self._pending[question.qid] = question
        self.events.put(question)
        while not question._done.wait(_POLL):
            if self._closed.is_set():
                with self._lock:
                    self._pending.pop(question.qid, None)
                return None
        return question._answer

    def _approve(self, plan: Plan) -> Sequence[Path | str] | None:
        answer = self._ask(Question(qid=next(self._ids), kind="approve", plan=plan,
                                    steps=describe_plan(plan)))
        if answer is None:
            return None
        if isinstance(answer, str | Path):
            # A bare string would otherwise be iterated character by character.
            return [answer]
        return [p for p in answer if isinstance(p, str | Path)]

    def _confirm(self, call: ToolCall, verdict: Verdict) -> bool:
        answer = self._ask(Question(qid=next(self._ids), kind="confirm",
                                    call=call, verdict=verdict))
        return answer is True

    # ------------------------------------------------- the worker thread

    def _work(self) -> None:
        try:
            session = self._factory(self._approve, self._confirm)
        except Exception as exc:
            self.events.put(Failed("start", f"the session could not open: {exc}"))
            return
        try:
            self.events.put(Ready(can_plan=session.can_plan,
                                  planner_error=session.planner_error,
                                  unfinished=session.unfinished(),
                                  history=session.history()))
            while True:
                command = self._commands.get()
                if command is None or self._closed.is_set():
                    return
                self._handle(session, *command)
        finally:
            session.close()

    def _handle(self, session: Session, action: str, arg: Any) -> None:
        try:
            if action == "run":
                token = CancelToken()
                with self._lock:
                    self._token = token
                try:
                    report = session.run(arg, cancel=token,
                                         progress=lambda e: self.events.put(Progress(e)))
                finally:
                    with self._lock:
                        self._token = None
                self.events.put(Finished(report, session.history()))
            elif action == "undo":
                report = session.undo(arg)
                self.events.put(Undone(report, session.history()))
            elif action == "clear":
                session.new_conversation()
                self.events.put(Cleared())
            elif action == "detail":
                self.events.put(Detail(session.plan_detail(arg)))
            elif action == "clear_history":
                session.clear_history()
                self.events.put(HistoryCleared(session.history()))
        except Exception as exc:
            # One bad command must not take the backend down with it.
            self.events.put(Failed(action, f"{type(exc).__name__}: {exc}"))
