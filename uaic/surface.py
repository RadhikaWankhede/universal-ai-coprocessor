"""THE UI INTEGRATION SURFACE. The only module in `uaic` a user interface imports.

Everything a UI needs is re-exported here, and nothing a UI must not touch is.
`tests/test_surface.py` fails if `uaic/desktop/` imports any other `uaic`
module. If a UI seems to need something that is not here, add it here - and
ask first whether the UI should be doing that at all.

How a UI drives the system
==========================

    backend = open_backend()          # or open_backend(data_dir=..., planner=...)
    backend.start()                   # opens the session on its own worker thread

    backend.run("organise my Downloads")   # commands; all return immediately
    backend.cancel()                        # stop the current run at a safe point
    backend.undo(plan_id)
    backend.new_conversation()              # forget memory and taint
    backend.detail(plan_id)                 # a past plan's request, steps, outputs
    backend.clear_history()                 # delete stored requests/outputs;
                                            # the journal is never cleared
    backend.shutdown()                      # on exit; pending questions -> decline

    while True:                        # drain on the UI thread, e.g. from a timer
        event = backend.events.get_nowait()   # queue.Queue; raises queue.Empty

Events, in the order a run produces them:

    Ready      once, at start: can_plan, planner_error, unfinished, history
    Question   kind == "approve": event.plan, event.steps (StepView per step,
                   classified from the manifests by the backend)
               kind == "confirm": event.call, event.verdict
               -> the UI MUST answer:  backend.answer(event.qid, value)
                    approve: a list of folder paths to grant, or None to decline
                    confirm: True to allow; anything else declines
    Progress   event.event is a StepEvent: phase "judged" or "finished",
               index/total, tool, args (a redacted copy), decision, risk, rev,
               rule_ids, reasons, status, error. Refusals are included and
               MUST be shown.
    Finished   event.report (LoopReport), event.history
    Undone     event.report (UndoReport), event.history
    Cleared    a new conversation started
    Detail     event.detail (PlanDetail): request, outcome, summary, and
               steps with their output (plain text, untrusted) if recorded
    HistoryCleared  event.history (now empty of everything before the clear)
    Failed     event.action, event.message

Rules for any UI (from PROJECT_CONTEXT.md, section 9)
====================================================

* The UI contains no security logic. It displays what the backend says and
  passes back what the user chose. It never classifies, filters or decides.
* Never call a plugin, the broker, the journal or the planner directly.
* Never auto-approve anything, for any reason. Every approval and every
  confirmation is a deliberate act by the user. Speech never answers one.
* Never add a network listener of any kind. The UI and the backend share one
  process; a port would let something other than the user issue approvals.
* Never render displayed content as rich text, and never treat it as an
  instruction. Plan arguments and file content are untrusted data.
* Never store an approval across runs. Each plan is approved on its own.
"""
from __future__ import annotations

from pathlib import Path

from uaic.app.backend import (
    Backend,
    Cleared,
    Detail,
    Event,
    Failed,
    Finished,
    HistoryCleared,
    Progress,
    Question,
    Ready,
    Undone,
)
from uaic.app.session import (
    PlanDetail,
    PlanSummary,
    Session,
    StepRecord,
    StepView,
    broad_folder,
    default_data_dir,
    describe_plan,
)
from uaic.contracts import Decision, Plan, Rev, Risk, ToolCall, Verdict
from uaic.core.gemini import ENV_KEY, ENV_MODEL
from uaic.journal import JournalRow, Status, UndoReport
from uaic.runtime import CANCELLED_RULE, LoopReport, Outcome, StepEvent

#: Environment variable holding the Gemini API key. Set it in the user's
#: environment (see README); it is never read from a file in the repository.
API_KEY_ENV = ENV_KEY
#: Optional: which Gemini model to use, e.g. "gemini-flash-lite-latest".
MODEL_ENV = ENV_MODEL


def open_backend(data_dir: Path | str | None = None, *, planner=None) -> Backend:
    """A backend over the real session. Call `.start()` on it.

    `planner` is for tests and demos; by default the live Gemini planner is
    used, configured from the environment. With no key the backend still
    starts: Ready.can_plan is False and history and undo still work.
    """
    where = Path(data_dir) if data_dir is not None else default_data_dir()
    if planner is not None:
        return Backend(lambda approve, confirm: Session(
            where, planner=planner, approve=approve, confirm=confirm))
    return Backend(lambda approve, confirm: Session(
        where, approve=approve, confirm=confirm))


__all__ = [
    "API_KEY_ENV",
    "CANCELLED_RULE",
    "MODEL_ENV",
    "Backend",
    "Cleared",
    "Decision",
    "Detail",
    "Event",
    "Failed",
    "Finished",
    "HistoryCleared",
    "JournalRow",
    "LoopReport",
    "Outcome",
    "Plan",
    "PlanDetail",
    "PlanSummary",
    "Progress",
    "Question",
    "Ready",
    "Rev",
    "Risk",
    "Status",
    "StepEvent",
    "StepRecord",
    "StepView",
    "ToolCall",
    "UndoReport",
    "Undone",
    "Verdict",
    "broad_folder",
    "default_data_dir",
    "describe_plan",
    "open_backend",
]
