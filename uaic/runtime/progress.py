"""Progress events and cooperative cancellation for a run.

Both are things a UI needs and both sit next to the security core, so each is
defined by what it must NOT be able to do.

PROGRESS IS OBSERVATIONAL ONLY.
    A progress sink is told what happened; it is never asked anything. It
    receives a frozen event holding a deep, redacted COPY of the arguments, so
    it cannot mutate the call that is about to be dispatched. Whatever it does -
    including raising - cannot change a verdict, an outcome or the journal:
    `emit` swallows its exceptions. Refusals are reported like everything else,
    because a UI that hides refusals misleads the user about what happened.

CANCELLATION IS COOPERATIVE AND CHECKED ONLY AT SAFE POINTS.
    The token is read at exactly two places: before a call is judged, and
    after it is judged (and any confirmation answered) but before it is
    journalled and dispatched. Never during a dispatch - a half-run move is
    worse than a finished one, and finished steps stay undoable.
    Cancelling does not answer a pending confirmation: the confirmer is the
    human's channel and the token never touches it. If the user approves a
    step and cancels, the step is still not dispatched - the safe point after
    the confirmation sees the cancel. Cancel can only ever make LESS happen.
"""
from __future__ import annotations

import contextlib
import copy
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from uaic.contracts import Decision, Rev, Risk, ToolCall, Verdict
from uaic.journal import redact

# The reason code a cancelled-before-dispatch call is journalled with.
CANCELLED_RULE = "RT.CANCELLED"


class CancelToken:
    """Set once by the user; read by the runtime at its safe points."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def is_cancelled(token: CancelToken | None) -> bool:
    return token is not None and token.cancelled


def cancelled_verdict(verdict: Verdict) -> Verdict:
    """How a call stopped at the pre-dispatch safe point is recorded: the
    broker's classification is kept, the decision becomes a refusal."""
    return Verdict(decision=Decision.DENY, risk=verdict.risk, rev=verdict.rev,
                   reasons=("cancelled by the user before dispatch",),
                   rule_ids=(CANCELLED_RULE, *verdict.rule_ids))


@dataclass(frozen=True)
class StepEvent:
    """One observation of one step. Built from copies; holds no live objects.

    phase:
      "judged"   - the broker (and taint escalation) has decided; nothing has
                   been asked or dispatched yet
      "finished" - the step is over: executed, failed, denied, declined or
                   cancelled. `status` says which.
    """
    phase: str
    plan_id: str
    call_id: str
    index: int                 # the step's number in the plan (retries share it)
    total: int                 # steps known at this moment; a re-plan can grow it
    tool: str
    args: dict[str, Any]       # redacted deep copy - display only
    decision: Decision
    risk: Risk
    rev: Rev
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    tainted: bool
    status: str | None = None
    error: str | None = None
    # What the step returned, as PLAIN TEXT for display: a file listing, the
    # text of a document, git output. It is untrusted content - a UI shows it
    # verbatim and never renders or obeys it. Capped at MAX_OUTPUT_CHARS.
    output: str | None = None


ProgressSink = Callable[[StepEvent], None]

MAX_OUTPUT_CHARS = 20_000


def display_output(payload: Any) -> str | None:
    """A plain-text copy of a tool's payload, for people to read."""
    if payload is None:
        return None
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, list | tuple):
        text = "\n".join(str(item) for item in payload) or "(empty)"
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_OUTPUT_CHARS:
        dropped = len(text) - MAX_OUTPUT_CHARS
        text = text[:MAX_OUTPUT_CHARS] + f"\n... [{dropped} more characters not shown]"
    return text


def make_event(phase: str, call: ToolCall, verdict: Verdict, *, total: int,
               status: str | None = None, error: str | None = None,
               output: str | None = None) -> StepEvent:
    return StepEvent(phase=phase, plan_id=call.plan_id, call_id=call.call_id,
                     index=call.seq, total=total, tool=call.tool,
                     args=copy.deepcopy(redact(dict(call.args))),
                     decision=verdict.decision, risk=verdict.risk, rev=verdict.rev,
                     rule_ids=tuple(verdict.rule_ids), reasons=tuple(verdict.reasons),
                     tainted=call.tainted, status=status, error=error, output=output)


def emit(sink: ProgressSink | None, event: StepEvent) -> None:
    """Deliver an event. A failing sink is ignored: it observes, it cannot veto."""
    if sink is None:
        return
    with contextlib.suppress(Exception):
        sink(event)
