"""The execution loop: broker -> journal -> plugin -> journal.

The Agent Core never appears here except as the source of a list of ToolCalls.
Nothing in this module can reach a plugin except through `decide` first.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from uaic.broker import CapabilitySet, Registry, decide, default_registry
from uaic.contracts import Decision, ToolCall, ToolResult, Verdict
from uaic.journal import ActionJournal, Status

# Asks the user. Returns True to proceed. A plan-level confirmation is asked
# once for the whole plan; a step-level one is asked for every call.
Confirmer = Callable[[ToolCall, Verdict], bool]

PluginExecute = Callable[[ToolCall], ToolResult]


def deny_everything(call: ToolCall, verdict: Verdict) -> bool:
    """Default confirmer: refuse anything needing a human. Safe for tests."""
    return False


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

    # ------------------------------------------------------------------ api

    def run_plan(self, calls: Sequence[ToolCall], caps: CapabilitySet) -> PlanReport:
        report = PlanReport(plan_id=caps.plan_id)
        plan_confirmed = False

        for call in calls:
            outcome, plan_confirmed, asked = self._run_one(call, caps, plan_confirmed)
            report.confirmations += asked
            report.steps.append(outcome)
            if not outcome.ran and self._stop_on_refusal:
                report.stopped_early = True
                break
        return report

    def run_one(self, call: ToolCall, caps: CapabilitySet) -> StepOutcome:
        outcome, _, _ = self._run_one(call, caps, plan_confirmed=False)
        return outcome

    # --------------------------------------------------------------- internals

    def _run_one(self, call: ToolCall, caps: CapabilitySet,
                 plan_confirmed: bool) -> tuple[StepOutcome, bool, int]:
        verdict = decide(call, caps, registry=self._registry,
                         session_root=self._session_root)
        asked = 0

        if verdict.decision is Decision.DENY:
            self._journal.write_refusal(call, verdict)
            return StepOutcome(call, verdict, status=Status.DENIED), plan_confirmed, asked

        if verdict.decision is Decision.CONFIRM_PLAN and not plan_confirmed:
            asked = 1
            if not self._confirm(call, verdict):
                self._journal.write_refusal(call, verdict, declined_by_user=True)
                return (StepOutcome(call, verdict, status=Status.DECLINED),
                        plan_confirmed, asked)
            plan_confirmed = True

        elif verdict.decision is Decision.CONFIRM_STEP:
            asked = 1
            if not self._confirm(call, verdict):
                self._journal.write_refusal(call, verdict, declined_by_user=True)
                return (StepOutcome(call, verdict, status=Status.DECLINED),
                        plan_confirmed, asked)

        spec = self._registry.lookup(call.tool)
        plugin_name = call.tool.split(".", 1)[0]
        run = self._plugins.get(plugin_name)
        if run is None:
            # Declared in a manifest but no plugin loaded: refuse, do not guess.
            self._journal.write_refusal(call, verdict)
            return (StepOutcome(call, verdict, status=Status.DENIED),
                    plan_confirmed, asked)

        # intent is committed BEFORE dispatch, so a crash leaves evidence
        self._journal.write_intent(call, verdict, mutates=spec.mutates)
        result = run(call)
        self._journal.write_outcome(result)

        return (StepOutcome(call, verdict, result=result,
                            status=Status.EXECUTED if result.ok else Status.FAILED),
                plan_confirmed, asked)
