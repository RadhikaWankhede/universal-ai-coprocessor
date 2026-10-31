"""Taint: was untrusted content in front of the planner when it proposed a call?

THE RULE (broad, deliberately)
A plan is tainted if ANY untrusted entry was in the memory the planner was
given - a tool result, a failure note, a refusal note - and its sources are
those entries' source_refs, in order, without repeats. Failure notes count
because they quote what failed, and that is a real injection channel: a git
hook's stderr reaches the planner that way.

THE ESCALATION
A call that is tainted AND not one of the calls the user approved moves up
one tier of the gate. The approved calls are exempt because the user saw
them, with their arguments, before any content was read.

    ALLOW         -> CONFIRM_PLAN   asked once; a retry is not re-asked
    CONFIRM_PLAN  -> CONFIRM_STEP   asked on every dispatch, retries included
    CONFIRM_STEP  -> CONFIRM_STEP   stays: the human already sees this exact call
    DENY          -> DENY

CONFIRM_STEP does not become DENY. A step-level question already puts the
exact call, with its arguments, in front of the user on every dispatch, so
DENY would add protection only against a user who approves without reading,
and taint is broad: it would hard-block every re-planned irreversible call
after any failure, which is ordinary recovery. DENY stays what the GATE
makes it - a property of the action (irreversible and critical), not of who
suggested it.

Like GATE, ESCALATE is literal data, not branching logic.
"""
from __future__ import annotations

from uaic.contracts import Decision, ToolCall, Verdict
from uaic.core.memory import SessionMemory

ESCALATE: dict[Decision, Decision] = {
    Decision.ALLOW:        Decision.CONFIRM_PLAN,
    Decision.CONFIRM_PLAN: Decision.CONFIRM_STEP,
    Decision.CONFIRM_STEP: Decision.CONFIRM_STEP,
    Decision.DENY:         Decision.DENY,
}

UNKNOWN_SOURCE = "unknown"


def taint_of(memory: SessionMemory) -> tuple[bool, tuple[str, ...]]:
    """(tainted, sources) for a planner about to be given `memory.context()`."""
    untrusted = [item for item in memory.items if not item.trusted]
    sources: list[str] = []
    for item in untrusted:
        ref = item.source_ref or UNKNOWN_SOURCE
        if ref not in sources:
            sources.append(ref)
    return bool(untrusted), tuple(sources)


def escalate(verdict: Verdict, call: ToolCall) -> Verdict:
    """The verdict one tier up, naming the sources. Unchanged if the tier is."""
    decision = ESCALATE[verdict.decision]
    if decision is verdict.decision:
        return verdict
    named = ", ".join(call.taint_sources) or UNKNOWN_SOURCE
    return verdict.model_copy(update={
        "decision": decision,
        "reasons": (*verdict.reasons,
                    f"escalated to {decision.value}: proposed after reading untrusted "
                    f"content ({named}) and not part of the approved plan"),
        "rule_ids": (*verdict.rule_ids, "RT.TAINT_ESCALATION"),
    })
