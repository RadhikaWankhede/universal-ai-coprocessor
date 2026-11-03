"""The Capability Broker: the only path from a proposed call to the machine.

    validate -> normalise -> scope -> classify -> gate

Stages 1-4 are pure functions: no network, no model, no side effects. That is
why the entire decision layer is testable from a table of cases.
"""
from __future__ import annotations

from pathlib import Path

from uaic.broker.capability import CapabilitySet, resolve
from uaic.broker.manifest import Registry, ToolSpec, default_registry
from uaic.broker.risk import classify
from uaic.contracts import Decision, Rev, Risk, ToolCall, Verdict

# (reversibility, risk) -> decision.  Policy as data, never as branching logic.
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


ARG_TYPES: dict[str, type] = {"str": str, "email": str, "bool": bool, "int": int}


def _deny(rule_id: str, reason: str) -> Verdict:
    # An unclassifiable call is treated as the worst case, never the best.
    return Verdict(decision=Decision.DENY, risk=Risk.CRITICAL,
                   rev=Rev.IRREVERSIBLE, reasons=(reason,), rule_ids=(rule_id,))


def _wrong_types(spec: ToolSpec, args: dict) -> list[str]:
    """Arguments whose value is not of the type the manifest declares.

    Path arguments are not listed here: every one is scoped, and stage 3 refuses
    a value that will not resolve. A declared-optional argument may be None.
    """
    wrong = []
    for name, value in args.items():
        arg = spec.args[name]
        if arg.type == "path" or (value is None and not arg.required):
            continue
        accepted = isinstance(value, ARG_TYPES[arg.type])
        if arg.type == "int" and isinstance(value, bool):   # bool is a subclass of int
            accepted = False
        if not accepted:
            wrong.append(name)
    return wrong


def decide(call: ToolCall, caps: CapabilitySet, *,
           registry: Registry | None = None,
           session_root: Path | None = None) -> Verdict:
    reg = registry or default_registry()

    # 1. declared?
    spec: ToolSpec | None = reg.lookup(call.tool)
    if spec is None:
        return _deny("BRK.UNDECLARED_TOOL", f"{call.tool!r} is not a declared tool")

    # 2. arguments valid against the manifest?
    missing = [a for a in spec.required_args() if a not in call.args]
    if missing:
        return _deny("BRK.BAD_ARGS", f"missing argument(s): {', '.join(sorted(missing))}")
    unexpected = [a for a in call.args if a not in spec.args]
    if unexpected:
        return _deny("BRK.BAD_ARGS",
                     f"undeclared argument(s): {', '.join(sorted(unexpected))}")

    wrong = _wrong_types(spec, call.args)
    if wrong:
        return _deny("BRK.BAD_ARGS",
                     f"argument(s) of the wrong type: {', '.join(sorted(wrong))}")

    # 3. normalise every scoped target to its real canonical path
    targets: list[Path] = []
    for a in spec.scope_args:
        t = resolve(call.args.get(a), session_root)
        if t is None:
            return _deny("BRK.UNRESOLVABLE",
                         f"could not resolve {a}={call.args.get(a)!r}")
        targets.append(t)

    # 4. inside the capability set frozen at approval?
    if not caps.permits_tool(call.tool):
        return _deny("BRK.OUTSIDE_FROZEN_CAPABILITY",
                     f"{call.tool} was not part of the approved plan")
    if call.seq >= caps.max_steps:
        return _deny("BRK.STEP_BUDGET_EXCEEDED",
                     f"step {call.seq} exceeds the plan budget of {caps.max_steps}")
    for a, t in zip(spec.scope_args, targets, strict=True):
        if not caps.covers_path(t):
            return _deny("BRK.OUT_OF_SCOPE",
                         f"{a} resolves outside the granted folders: {t}")

    # 5. how bad is it if this is wrong?
    risk, rev = classify(spec, call.args, tuple(targets))

    # 6. policy lookup
    return Verdict(
        decision=GATE[(rev, risk)],
        risk=risk,
        rev=rev,
        reasons=(f"{call.tool} classified {rev.value}/{risk.value}",),
        rule_ids=("BRK.GATE",),
    )
