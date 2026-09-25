"""The agent loop: propose, approve, freeze, execute, observe, recover.

This is FR-5 - "inspect tool results, detect failures, retry, re-plan or ask
the user" - and it is where the project's central rule has to hold under
pressure, because re-planning is exactly the moment a naive implementation
widens its own permissions.

The rule it enforces:

    A RE-PLAN MAY NEVER WIDEN THE FROZEN CAPABILITY SET.

If the new plan needs a tool the user did not approve, the loop stops and
returns NEEDS_APPROVAL. It does not mint a wider capability set, and it does
not silently drop the offending step and carry on - either would leave the
user with a false belief about what happened. Escalation goes back to the
human, who approves a NEW plan with a NEW id.

Everything the loop does is bounded: retries per step, re-plans per run, and
the step budget already inside the capability set. An agent loop without
bounds is not an agent, it is a while-true with an API bill.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from uaic.broker import CapabilitySet
from uaic.contracts import Decision, Plan, ToolCall, Verdict
from uaic.core.llm import PlannerError
from uaic.core.memory import SessionMemory
from uaic.journal import Status
from uaic.runtime.executor import Executor, StepOutcome
from uaic.runtime.freeze import escalation_needed, freeze_plan

MAX_RETRIES = 2         # per step
MAX_REPLANS = 1         # per run
STEP_HEADROOM = 2       # slack on top of the loop's declared worst case

# Returns the folders the user grants, or None to decline the plan outright.
Approver = Callable[[Plan], Sequence[Path | str] | None]
Confirmer = Callable[[ToolCall, Verdict], bool]


class Outcome(StrEnum):
    COMPLETED = "completed"
    DECLINED = "declined"                # the user refused the plan or a step
    REFUSED = "refused"                  # the broker refused a step
    NEEDS_APPROVAL = "needs_approval"    # a re-plan wanted more than was granted
    EXHAUSTED = "exhausted"              # retries and re-plans ran out
    PLANNING_FAILED = "planning_failed"  # the model produced no usable plan


@dataclass
class LoopReport:
    request: str
    outcome: Outcome
    plan: Plan | None = None
    caps: CapabilitySet | None = None
    steps: list[StepOutcome] = field(default_factory=list)
    confirmations: int = 0
    retries: int = 0
    replans: int = 0
    escalation: frozenset[str] = frozenset()
    message: str = ""

    @property
    def executed(self) -> list[StepOutcome]:
        return [s for s in self.steps if s.status == Status.EXECUTED]

    def describe(self) -> str:
        parts = [f"{self.outcome.value}: {len(self.executed)}/{len(self.steps)} "
                 f"step(s) executed"]
        if self.confirmations:
            parts.append(f"{self.confirmations} confirmation(s)")
        if self.retries:
            parts.append(f"{self.retries} retry/retries")
        if self.replans:
            parts.append(f"{self.replans} re-plan(s)")
        if self.escalation:
            parts.append(f"needs approval for {sorted(self.escalation)}")
        if self.message:
            parts.append(self.message)
        return "; ".join(parts)


class _AskOncePerPlan:
    """Wraps the user's confirmer so a plan-level question is asked once.

    The executor is stateless across `run_one` calls by design, and the loop
    drives it one step at a time so it can observe each result. Without this
    wrapper the user would be asked the same plan-level question on every
    compensable step, which is the fastest way to train someone to click yes.
    """

    def __init__(self, confirm: Confirmer) -> None:
        self._confirm = confirm
        self._plan_confirmed = False
        self.asked = 0

    def __call__(self, call: ToolCall, verdict: Verdict) -> bool:
        if verdict.decision is Decision.CONFIRM_PLAN and self._plan_confirmed:
            return True
        self.asked += 1
        answer = bool(self._confirm(call, verdict))
        if answer and verdict.decision is Decision.CONFIRM_PLAN:
            self._plan_confirmed = True
        return answer


def always_decline(plan: Plan) -> None:
    """Default approver: grants nothing. Safe for tests and for a headless run."""
    return None


class AgentLoop:
    def __init__(self, planner, executor_factory: Callable[[Confirmer], Executor],
                 *,
                 approve: Approver = always_decline,
                 confirm: Confirmer | None = None,
                 memory: SessionMemory | None = None,
                 max_retries: int = MAX_RETRIES,
                 max_replans: int = MAX_REPLANS) -> None:
        """`executor_factory` builds an Executor around a confirmer.

        The loop has to install its own ask-once wrapper, and an Executor is
        constructed with its confirmer, so the loop is handed the recipe rather
        than a finished object.
        """
        self._planner = planner
        self._make_executor = executor_factory
        self._approve = approve
        self._confirm = confirm or (lambda call, verdict: False)
        self.memory = memory or SessionMemory()
        self._max_retries = max_retries
        self._max_replans = max_replans

    # ------------------------------------------------------------------- run

    def run(self, request: str, plan_id: str) -> LoopReport:
        self.memory.user_said(request)
        report = LoopReport(request=request, outcome=Outcome.PLANNING_FAILED)

        try:
            plan = self._planner.propose(request, plan_id,
                                         context=self.memory.context())
        except PlannerError as exc:
            report.message = str(exc)
            self.memory.noted(f"planning failed: {exc}")
            return report

        report.plan = plan

        granted = self._approve(plan)
        if granted is None:
            report.outcome = Outcome.DECLINED
            report.message = "the user did not approve the plan"
            self.memory.noted("the user declined the plan")
            return report

        caps = freeze_plan(plan, granted, max_steps=self._budget(plan))
        report.caps = caps

        asker = _AskOncePerPlan(self._confirm)
        executor = self._make_executor(asker)

        pending: list[ToolCall] = list(plan.to_calls())
        attempts: dict[str, int] = {}

        while pending:
            call = pending.pop(0)
            outcome = executor.run_one(call, caps)
            report.steps.append(outcome)
            self._remember(outcome)

            if outcome.status == Status.EXECUTED:
                continue

            if outcome.status in (Status.DENIED, Status.DECLINED):
                # A refusal is final. Retrying a denial would be a retry loop
                # against the security layer, which is never the right answer.
                report.outcome = (Outcome.DECLINED
                                  if outcome.status == Status.DECLINED
                                  else Outcome.REFUSED)
                report.message = (outcome.verdict.reasons[0]
                                  if outcome.verdict.reasons else "")
                report.confirmations = asker.asked
                return report

            # The step ran and failed: a locked file, a missing path, a bad
            # argument. This is the normal case on a real machine.
            tried = attempts.get(call.call_id, 0) + 1
            attempts[call.call_id] = tried
            if tried <= self._max_retries:
                report.retries += 1
                pending.insert(0, call.model_copy(update={
                    "call_id": f"{call.call_id}-retry{tried}"}))
                attempts[f"{call.call_id}-retry{tried}"] = tried
                continue

            replanned = self._replan(report, caps, plan, outcome)
            if replanned is None:
                report.confirmations = asker.asked
                return report
            pending = replanned

        report.outcome = Outcome.COMPLETED
        report.confirmations = asker.asked
        return report

    # ------------------------------------------------------------- internals

    def _budget(self, plan: Plan) -> int:
        """The step budget the capability set will enforce.

        Found by a test: the default budget is the plan's length plus a little
        headroom, which a legitimate re-plan overruns - recovery steps continue
        the sequence, so they consume budget. The loop therefore declares its
        own worst case up front, and the capability set still enforces it. A
        retry does NOT consume budget, because it reuses its step's number.
        """
        return len(plan.steps) * (1 + self._max_replans) + STEP_HEADROOM

    def _remember(self, outcome: StepOutcome) -> None:
        if outcome.ran and outcome.result.ok:
            self.memory.observed(outcome.call.tool, outcome.result.payload)
        elif outcome.ran:
            self.memory.noted(f"{outcome.call.tool} failed: {outcome.result.error}")
        else:
            rule = outcome.verdict.rule_ids[0] if outcome.verdict.rule_ids else "?"
            self.memory.noted(f"{outcome.call.tool} was refused ({rule})")

    def _replan(self, report: LoopReport, caps: CapabilitySet, plan: Plan,
                failed: StepOutcome) -> list[ToolCall] | None:
        """Ask for a new plan after a step kept failing.

        Returns the remaining calls to run, or None if the loop must stop.
        """
        if report.replans >= self._max_replans:
            report.outcome = Outcome.EXHAUSTED
            report.message = (f"{failed.call.tool} failed after "
                              f"{self._max_retries} retries")
            return None

        error = failed.result.error if failed.ran else "refused"
        try:
            revised = self._planner.propose(
                f"The step {failed.call.tool} failed: {error}. "
                f"Propose how to finish the original request.",
                f"{plan.plan_id}-r{report.replans + 1}",
                context=self.memory.context())
        except PlannerError as exc:
            report.outcome = Outcome.EXHAUSTED
            report.message = f"re-planning failed: {exc}"
            return None

        report.replans += 1

        needed = escalation_needed(revised, caps)
        if needed:
            # The new plan wants something the user never granted. This is the
            # single most important branch in the file: stop, and hand the
            # decision back to the human.
            report.outcome = Outcome.NEEDS_APPROVAL
            report.escalation = needed
            report.message = ("the revised plan needs tools that were not "
                              f"approved: {sorted(needed)}")
            self.memory.noted(f"escalation required for {sorted(needed)}")
            return None

        # The revised steps run under the EXISTING capability set, and are
        # journalled under the existing plan id so the audit trail stays one
        # story rather than two.
        offset = len(report.steps)
        return [c.model_copy(update={"plan_id": caps.plan_id,
                                     "seq": offset + i + 1,
                                     "call_id": f"{caps.plan_id}-replan{report.replans}"
                                                f"-{i + 1}"})
                for i, c in enumerate(revised.to_calls())]
