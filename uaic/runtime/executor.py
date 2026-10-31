"""The execution loop: broker -> journal -> plugin -> journal.

The Agent Core never appears here except as the source of a list of ToolCalls.
Nothing in this module can reach a plugin except through `decide` first.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from uaic.broker import CapabilitySet, Registry, decide, default_registry
from uaic.contracts import Decision, Rev, Risk, ToolCall, ToolResult, Verdict
from uaic.journal import ActionJournal, JournalError, Status
from uaic.runtime.binding import ConfirmationLedger

# Asks the user. Returns True to proceed. A plan-level confirmation covers the
# calls of the approved plan (see binding.py); a step-level one is asked for
# every dispatch.
Confirmer = Callable[[ToolCall, Verdict], bool]

PluginExecute = Callable[[ToolCall], ToolResult]


def deny_everything(call: ToolCall, verdict: Verdict) -> bool:
    """Default confirmer: refuse anything needing a human. Safe for tests."""
    return False


def _not_journalled(reason: str) -> Verdict:
    """Nothing reaches the machine without an audit entry. If the journal will
    not accept the record, the action does not happen."""
    return Verdict(decision=Decision.DENY, risk=Risk.CRITICAL,
                   rev=Rev.IRREVERSIBLE,
                   reasons=(f"refused: could not be journalled ({reason})",),
                   rule_ids=("RT.NOT_JOURNALLED",))


@dataclass
class StepOutcome:
    call: ToolCall
    verdict: Verdict
    result: ToolResult | None = None
    status: str = Status.DENIED

    @property
    def ran(self) -> bool:
        return self.result is not None


@dataclass
class PlanReport:
    plan_id: str
    steps: list[StepOutcome] = field(default_factory=list)
    confirmations: int = 0
    stopped_early: bool = False

    @property
    def executed(self) -> list[StepOutcome]:
        return [s for s in self.steps if s.status == Status.EXECUTED]

    @property
    def refused(self) -> list[StepOutcome]:
        return [s for s in self.steps
                if s.status in (Status.DENIED, Status.DECLINED)]

    def describe(self) -> str:
        return (f"{len(self.executed)}/{len(self.steps)} step(s) executed, "
                f"{len(self.refused)} refused, "
                f"{self.confirmations} confirmation(s) asked"
                + (" (stopped early)" if self.stopped_early else ""))


class Executor:
    def __init__(self, journal: ActionJournal,
                 plugins: dict[str, PluginExecute],
                 *,
                 confirm: Confirmer = deny_everything,
                 registry: Registry | None = None,
                 session_root: Path | None = None,
                 stop_on_refusal: bool = False) -> None:
        self._journal = journal
        self._plugins = dict(plugins)
        self._confirm = confirm
        self._registry = registry or default_registry()
        self._session_root = session_root
        self._stop_on_refusal = stop_on_refusal

    def ledger_for(self, approved: Sequence[ToolCall]) -> ConfirmationLedger:
        """A confirmation ledger that compares paths exactly as this executor's
        broker calls will - same registry, same session root."""
        return ConfirmationLedger(approved, self._registry, self._session_root)

    # ------------------------------------------------------------------ api

    def run_plan(self, calls: Sequence[ToolCall], caps: CapabilitySet) -> PlanReport:
        report = PlanReport(plan_id=caps.plan_id)
        ledger = self.ledger_for(calls)

        for call in calls:
            outcome, asked = self._run_one(call, caps, ledger)
            report.confirmations += asked
            report.steps.append(outcome)
            if not outcome.ran and self._stop_on_refusal:
                report.stopped_early = True
                break
        return report

    def run_one(self, call: ToolCall, caps: CapabilitySet,
                ledger: ConfirmationLedger | None = None) -> StepOutcome:
        """Without a ledger, every plan-level question is asked."""
        outcome, _ = self._run_one(call, caps, ledger)
        return outcome

    # --------------------------------------------------------------- internals

    def _record_refusal(self, call: ToolCall, verdict: Verdict, *,
                        declined: bool = False) -> None:
        """Nothing ran, so failing to record the refusal must not crash the run."""
        with contextlib.suppress(JournalError):
            self._journal.write_refusal(call, verdict, declined_by_user=declined)

    def _run_one(self, call: ToolCall, caps: CapabilitySet,
                 ledger: ConfirmationLedger | None) -> tuple[StepOutcome, int]:
        verdict = decide(call, caps, registry=self._registry,
                         session_root=self._session_root)
        asked = 0

        if verdict.decision is Decision.DENY:
            self._record_refusal(call, verdict)
            return StepOutcome(call, verdict, status=Status.DENIED), asked

        if verdict.decision is Decision.CONFIRM_PLAN and not (ledger and ledger.covers(call)):
            asked = 1
            if not self._confirm(call, verdict):
                self._record_refusal(call, verdict, declined=True)
                return StepOutcome(call, verdict, status=Status.DECLINED), asked
            if ledger is not None:
                ledger.confirmed(call)

        elif verdict.decision is Decision.CONFIRM_STEP:
            asked = 1
            if not self._confirm(call, verdict):
                self._record_refusal(call, verdict, declined=True)
                return StepOutcome(call, verdict, status=Status.DECLINED), asked

        spec = self._registry.lookup(call.tool)
        plugin_name = call.tool.split(".", 1)[0]
        run = self._plugins.get(plugin_name)
        if run is None:
            # Declared in a manifest but no plugin loaded: refuse, do not guess.
            self._record_refusal(call, verdict)
            return StepOutcome(call, verdict, status=Status.DENIED), asked

        # intent is committed BEFORE dispatch, so a crash leaves evidence.
        # If it cannot be committed, the call is refused rather than run blind.
        try:
            self._journal.write_intent(call, verdict, mutates=spec.mutates)
        except JournalError as exc:
            refused = _not_journalled(str(exc))
            return StepOutcome(call, refused, status=Status.DENIED), asked

        result = run(call)
        # the intent row survives regardless; an unrecorded outcome is not fatal
        with contextlib.suppress(JournalError):
            self._journal.write_outcome(result)
        if result.ok and ledger is not None:
            ledger.executed(call)       # spent on success; a failure keeps it for the retry

        return (StepOutcome(call, verdict, result=result,
                            status=Status.EXECUTED if result.ok else Status.FAILED),
                asked)
