"""The composition root: one user's session, wired from the real parts.

Everything the desktop application does goes through this class, and this
class contains no UI code - so the whole session, live planner aside, is
exercised by the test suite without a display.

Two rules shape it:

* The journal's SQLite connection belongs to the thread that opened it, so a
  Session is built and used on ONE thread. The desktop app builds it on its
  backend worker thread (see backend.py) and never touches it from the UI.

* `approve` and `confirm` are the only routes by which a human says yes. They
  are injected, never defaulted to anything that grants: with no approver the
  session declines every plan.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from uaic.app.records import RunRecords
from uaic.broker import CapabilitySet, default_registry
from uaic.contracts import Origin, Plan, Rev, Risk, ToolCall, ToolResult
from uaic.core import GeminiPlanner, HttpTransport, LLMError, SessionMemory
from uaic.core.gemini import ENV_KEY
from uaic.core.llm import api_key
from uaic.journal import ActionJournal, JournalRow, Status, UndoEngine, UndoReport
from uaic.plugins import DocumentsPlugin, FileSystemPlugin, GitPlugin, TerminalPlugin
from uaic.runtime import (
    AgentLoop,
    CancelToken,
    Executor,
    LoopReport,
    Outcome,
    ProgressSink,
    StepEvent,
    always_decline,
)
from uaic.runtime.executor import PluginExecute
from uaic.runtime.loop import Approver, Confirmer

UNDO_PREFIX = "undo:"   # journal plan ids of undo runs, kept out of the history list
PLANNER_TIMEOUT = 120   # seconds per attempt; the live flash model is sometimes slow


def default_data_dir() -> Path:
    """Where the journal and the trash live: per-user, outside any repo."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "uaic") if base else (Path.home() / ".uaic")


def live_planner() -> GeminiPlanner:
    """The Gemini planner, configured as scripts/live_check.py proved it works.

    Raises LLMError if no key is set - resolved here, at startup, so the user
    is told before typing a request rather than after.
    """
    key = api_key(ENV_KEY)
    transport = HttpTransport(secrets=(key,), max_attempts=2, backoff=3.0)
    return GeminiPlanner(api_key_value=key, transport=transport,
                         timeout=PLANNER_TIMEOUT)


def new_plan_id() -> str:
    """Unique across runs: the journal persists, so plan ids must never repeat."""
    return f"p-{uuid.uuid4().hex[:12]}"


UNDO_TEXT = {
    Rev.REVERSIBLE: "can be undone",
    Rev.COMPENSABLE: "can be compensated, not exactly undone",
    Rev.IRREVERSIBLE: "CANNOT be undone",
}


@dataclass(frozen=True)
class StepView:
    """One plan step as the approval screen shows it.

    The risk and reversibility come from the tool's MANIFEST - the same data
    the broker gates on - and are looked up here, on the backend side, so the
    UI never classifies anything itself. An undeclared tool is shown as such:
    the broker will refuse it, and the user should see that before approving.
    """
    seq: int
    tool: str
    summary: str
    args: dict
    rationale: str
    declared: bool
    risk: Risk | None
    rev: Rev | None
    mutates: bool
    undo: str


def describe_plan(plan: Plan) -> tuple[StepView, ...]:
    registry = default_registry()
    views = []
    for step in plan.steps:
        spec = registry.lookup(step.tool)
        views.append(StepView(
            seq=step.seq, tool=step.tool,
            summary=spec.summary if spec else "not a declared tool - will be refused",
            args=dict(step.args), rationale=step.rationale,
            declared=spec is not None,
            risk=spec.risk if spec else None, rev=spec.rev if spec else None,
            mutates=spec.mutates if spec else True,
            undo=(UNDO_TEXT[spec.rev] if spec and spec.mutates else
                  "changes nothing" if spec else "will be refused")))
    return tuple(views)


def broad_folder(path: str) -> bool:
    """A drive root or the user's home folder: a grant worth a second look.

    Advisory only - shown on the approval screen, never used to decide
    anything. An unresolvable path is reported as broad, to be safe.
    """
    try:
        p = Path(path).expanduser().resolve()
        return p == Path(p.anchor) or p == Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        return True


@dataclass(frozen=True)
class PlanSummary:
    """One line of history, as the UI lists it."""
    plan_id: str
    started: str
    tools: tuple[str, ...]
    executed: int
    undone: int
    refused: int
    request: str = ""          # from records.db; empty for plans from before it existed
    outcome: str = ""

    @property
    def undoable(self) -> bool:
        return self.executed > 0


@dataclass(frozen=True)
class StepRecord:
    """One journalled step of a past plan, with its output if it was recorded."""
    seq: int
    call_id: str
    tool: str
    args: dict
    decision: str
    status: str
    error: str | None
    output: str | None         # plain text, untrusted; None if not recorded


@dataclass(frozen=True)
class PlanDetail:
    """Everything the app can show about a past plan."""
    plan_id: str
    started: str
    request: str
    outcome: str
    summary: str
    preview: str
    steps: tuple[StepRecord, ...]
    outputs_recorded: bool     # False for plans run before records.db existed


class Session:
    def __init__(self, data_dir: Path | str, *,
                 planner=None,
                 planner_factory: Callable[[], object] | None = live_planner,
                 approve: Approver = always_decline,
                 confirm: Confirmer | None = None,
                 plugins: dict[str, PluginExecute] | None = None) -> None:
        """`planner` wins over `planner_factory`; tests pass a planner.

        If the factory fails (no API key), the session still opens - history
        and undo work offline - and `planner_error` says why planning will not.
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal = ActionJournal(self.data_dir / "journal.db")
        self.records = RunRecords(self.data_dir / "records.db")
        self._plugins = plugins or {
            "fs": FileSystemPlugin(self.data_dir / "trash").execute,
            "git": GitPlugin().execute,
            "doc": DocumentsPlugin().execute,
            "terminal": TerminalPlugin().execute,
            # mail is deliberately absent: the app has no mail plugin, so a
            # mail.* call can never be dispatched from here.
        }

        self.planner_error: str | None = None
        if planner is None and planner_factory is not None:
            try:
                planner = planner_factory()
            except LLMError as exc:
                self.planner_error = str(exc)
        if planner is None and self.planner_error is None:
            self.planner_error = "no planner is configured"
        self._planner = planner

        self.memory = SessionMemory()
        self._approve = approve
        self._confirm = confirm or (lambda call, verdict: False)
        self._loop = self._new_loop() if planner is not None else None

    # ------------------------------------------------------------------ run

    @property
    def can_plan(self) -> bool:
        return self._loop is not None

    def run(self, request: str, *, progress: ProgressSink | None = None,
            cancel: CancelToken | None = None) -> LoopReport:
        request = request.strip()
        if not request:
            return LoopReport(request=request, outcome=Outcome.PLANNING_FAILED,
                              message="the request is empty")
        if self._loop is None:
            return LoopReport(request=request, outcome=Outcome.PLANNING_FAILED,
                              message=self.planner_error or "no planner")
        plan_id = new_plan_id()
        self.records.begin(plan_id, request)

        def observe(event: StepEvent) -> None:
            # Keep what each step returned, so it can be read again later.
            if event.phase == "finished" and event.output is not None:
                with contextlib.suppress(sqlite3.Error):
                    self.records.output(event.call_id, event.plan_id, event.index,
                                        event.tool, event.output)
            if progress is not None:
                progress(event)

        try:
            report = self._loop.run(request, plan_id, progress=observe, cancel=cancel)
        except LLMError as exc:
            # PlannerError is handled inside the loop; its parent LLMError
            # (e.g. a key revoked mid-session) is not, and must not kill the
            # session. The text is already key-scrubbed by the transport.
            report = LoopReport(request=request, outcome=Outcome.PLANNING_FAILED,
                                message=str(exc))
        with contextlib.suppress(sqlite3.Error):
            self.records.finish(plan_id, report.outcome.value, report.describe(),
                                report.plan.preview() if report.plan else None)
        return report

    def new_conversation(self) -> None:
        """Forget what has been said and read. This also clears taint: a new
        conversation has read nothing untrusted yet."""
        self.memory = SessionMemory()
        if self._planner is not None:
            self._loop = self._new_loop()

    # ----------------------------------------------------------------- undo

    def undo(self, plan_id: str) -> UndoReport:
        return UndoEngine(self.journal,
                          lambda call: self._undo_call(plan_id, call)).undo_plan(plan_id)

    def _undo_grants(self, plan_id: str) -> tuple[str, ...]:
        """What the user approved for this plan. A plan from before grants were
        recorded falls back to the paths its own executed steps were admitted on."""
        granted = self.journal.grants(plan_id)
        if granted is not None:
            return granted
        registry = default_registry()
        paths: list[str] = []
        for row in self.journal.rows(plan_id, status=Status.EXECUTED):
            spec = registry.lookup(row.tool)
            for arg in spec.scope_args if spec else ():
                if isinstance(row.args.get(arg), str):
                    paths.append(row.args[arg])
        return tuple(paths)

    def _undo_call(self, plan_id: str, call: ToolCall) -> ToolResult:
        """Run one inverse like any other call: broker, confirmation, journal, plugin.

        It is judged against the plan's own granted folders and may use only its
        own tool, so a recorded inverse cannot reach anywhere the user did not
        approve. It is journalled under `undo:<plan_id>`: a row under the plan's
        own id would look like a forward step and be replayed by a second undo.
        """
        undo_id = f"{UNDO_PREFIX}{plan_id}"
        brokered = call.model_copy(update={
            "plan_id": undo_id,
            "call_id": f"undo-{uuid.uuid4().hex[:12]}",   # a failed undo can be retried
            "origin": Origin.UNDO})
        caps = CapabilitySet(plan_id=undo_id, tools=frozenset({call.tool}),
                             path_grants=tuple(Path(p) for p in self._undo_grants(plan_id)),
                             max_steps=call.seq + 1)
        executor = Executor(self.journal, self._plugins, confirm=self._confirm)
        outcome = executor.run_one(brokered, caps)
        if outcome.result is not None:
            return outcome.result
        why = "; ".join(outcome.verdict.reasons) or outcome.status
        return ToolResult(call_id=brokered.call_id, ok=False, error=f"{outcome.status}: {why}")

    # -------------------------------------------------------------- history

    def history(self, limit: int = 50) -> list[PlanSummary]:
        """Recent plans, newest first: the journal's steps, joined with the
        stored request and outcome. Plans that never reached the journal (a
        declined plan, a planning failure) are listed from the records alone.
        Plans from before the last "clear history" are hidden - not deleted:
        their journal rows remain as the audit trail."""
        grouped: dict[str, list[JournalRow]] = {}
        for row in self.journal.rows():
            if row.plan_id.startswith(UNDO_PREFIX):
                continue
            grouped.setdefault(row.plan_id, []).append(row)
        records = self.records.plans()
        cleared = self.records.cleared_at()

        summaries = []
        for plan_id in grouped.keys() | records.keys():
            rows = grouped.get(plan_id, [])
            rec = records.get(plan_id)
            statuses = [r.status for r in rows]
            started = (rec["ts"] if rec is not None
                       else min(r.ts_intent for r in rows))
            if cleared is not None and started <= cleared:
                continue
            summaries.append(PlanSummary(
                plan_id=plan_id,
                started=started,
                tools=tuple(r.tool for r in sorted(rows, key=lambda r: r.seq)),
                executed=statuses.count(Status.EXECUTED),
                undone=statuses.count(Status.UNDONE),
                refused=statuses.count(Status.DENIED) + statuses.count(Status.DECLINED),
                request=rec["request"] if rec is not None else "",
                outcome=(rec["outcome"] or "") if rec is not None else "",
            ))
        summaries.sort(key=lambda s: s.started, reverse=True)
        return summaries[:limit]

    def plan_detail(self, plan_id: str) -> PlanDetail:
        """A past plan: what was asked, each journalled step, what each returned."""
        rec = self.records.plan(plan_id)
        outputs = self.records.outputs(plan_id)
        rows = self.journal.rows(plan_id)
        steps = tuple(StepRecord(seq=r.seq, call_id=r.call_id, tool=r.tool,
                                 args=dict(r.args), decision=r.decision.value,
                                 status=r.status, error=r.error,
                                 output=outputs.get(r.call_id))
                      for r in rows)
        return PlanDetail(
            plan_id=plan_id,
            started=(rec["ts"] if rec is not None
                     else min((r.ts_intent for r in rows), default="")),
            request=rec["request"] if rec is not None else "",
            outcome=(rec["outcome"] or "") if rec is not None else "",
            summary=(rec["summary"] or "") if rec is not None else "",
            preview=(rec["preview"] or "") if rec is not None else "",
            steps=steps,
            outputs_recorded=rec is not None)

    def clear_history(self) -> None:
        """Forget stored requests and outputs, and hide every plan so far.

        The journal is NOT touched: it is append-only by design (a trigger
        refuses deletes), and the audit trail must survive a tidy-up.
        """
        self.records.clear()

    def unfinished(self) -> list[JournalRow]:
        """Intents with no outcome: what was running when the app last died."""
        return self.journal.unfinished()

    def close(self) -> None:
        self.records.close()
        self.journal.close()

    # ------------------------------------------------------------ internals

    def _new_loop(self) -> AgentLoop:
        journal, plugins = self.journal, self._plugins
        return AgentLoop(
            self._planner,
            lambda asker: Executor(journal, plugins, confirm=asker),
            approve=self._approve,
            confirm=self._confirm,
            memory=self.memory)
