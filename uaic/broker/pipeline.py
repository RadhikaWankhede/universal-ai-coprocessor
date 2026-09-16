"""The Capability Broker: the only path from a proposed call to the machine.

Stages 1-5 are pure functions. No network, no model, no side effects -
which is why the whole decision layer is testable from a table of cases.
"""
from __future__ import annotations

from pathlib import Path

from uaic.broker.capability import CapabilitySet, resolve
from uaic.broker.risk import TOOL_SPECS, classify
from uaic.contracts import Decision, Rev, Risk, ToolCall, Verdict

# (reversibility, risk) -> decision.  Policy as data, not as branching logic.
GATE: dict[tuple[Rev, Risk], Decision] = {
    (Rev.REVERSIBLE,   Risk.LOW):      Decision.ALLOW,
    (Rev.REVERSIBLE,   Risk.MEDIUM):   Decision.ALLOW,
    (Rev.REVERSIBLE,   Risk.HIGH):     Decision.ALLOW,
    (Rev.REVERSIBLE,   Risk.CRITICAL): Decision.CONFIRM_STEP,
    (Rev.COMPENSABLE,  Risk.LOW):      Decision.CONFIRM_PLAN,
    (Rev.COMPENSABLE,  Risk.MEDIUM):   Decision.CONFIRM_PLAN,
    (Rev.COMPENSABLE,  Risk.HIGH):     Decision.CONFIRM_STEP,
    (Rev.COMPENSABLE,  Risk.CRITICAL): Decision.CONFIRM_STEP,
    (Rev.IRREVERSIBLE, Risk.LOW):      Decision.CONFIRM_STEP,
    (Rev.IRREVERSIBLE, Risk.MEDIUM):   Decision.CONFIRM_STEP,
    (Rev.IRREVERSIBLE, Risk.HIGH):     Decision.CONFIRM_STEP,
    (Rev.IRREVERSIBLE, Risk.CRITICAL): Decision.DENY,
}


def _deny(rule_id: str, reason: str) -> Verdict:
    # Conservative defaults: an unclassifiable call is treated as the worst case.
    return Verdict(
        decision=Decision.DENY,
        risk=Risk.HIGH,
        rev=Rev.IRREVERSIBLE,
        reasons=(reason,),
        rule_ids=(rule_id,),
    )


def decide(call: ToolCall, caps: CapabilitySet,
           session_root: Path | None = None) -> Verdict:
    # 1. is the tool declared at all?
    spec = TOOL_SPECS.get(call.tool)
    if spec is None:
        return _deny("BRK.UNDECLARED_TOOL", f"{call.tool!r} is not a declared tool")

    _, _, scope_args = spec

    # 2. are the arguments we need actually present?
    missing = [a for a in scope_args if a not in call.args]
    if missing:
        return _deny("BRK.BAD_ARGS", f"missing argument(s): {', '.join(missing)}")

    # 3. normalise every target to its real canonical path
    targets: list[Path] = []
    for a in scope_args:
        t = resolve(call.args[a], session_root)
        if t is None:
            return _deny("BRK.UNRESOLVABLE", f"could not resolve {a}={call.args[a]!r}")
        targets.append(t)

    # 4. is this inside the capability set the user froze at approval?
    if not caps.permits_tool(call.tool):
        return _deny("BRK.OUTSIDE_FROZEN_CAPABILITY",
                     f"{call.tool} was not part of the approved plan")
    for a, t in zip(scope_args, targets):
        if not caps.covers_path(t):
            return _deny("BRK.OUT_OF_SCOPE", f"{a} resolves outside the granted folders: {t}")

    # 5. how bad is it if this is wrong?
    risk, rev = classify(call.tool, call.args, tuple(targets))

    # 6. map (reversibility, risk) to a decision
    decision = GATE[(rev, risk)]
    return Verdict(
        decision=decision,
        risk=risk,
        rev=rev,
        reasons=(f"{call.tool} classified {rev.value}/{risk.value}",),
        rule_ids=("BRK.GATE",),
    )
