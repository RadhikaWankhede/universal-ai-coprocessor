"""Table-driven broker decisions."""
from __future__ import annotations

import pytest

from uaic.broker import CapabilitySet, decide
from uaic.contracts import Decision, Rev, Risk, ToolCall

N = 0


def call(tool: str, **args) -> ToolCall:
    global N
    N += 1
    return ToolCall(call_id=f"c{N}", plan_id="p1", tool=tool, args=args, seq=N)


# ---------- denials ----------

def test_undeclared_tool_is_denied(caps, sandbox):
    v = decide(call("fs.teleport", path=str(sandbox["downloads"])), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.UNDECLARED_TOOL",)


def test_missing_argument_is_denied(caps):
    v = decide(call("fs.move", src="x"), caps)   # dst missing
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.BAD_ARGS",)


def test_unresolvable_argument_is_denied(caps):
    v = decide(call("fs.list", path=""), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.UNRESOLVABLE",)


def test_tool_outside_frozen_set_is_denied(sandbox):
    narrow = CapabilitySet(plan_id="p1", tools=frozenset({"fs.read"}),
                           path_grants=(sandbox["downloads"],))
    v = decide(call("fs.delete", path=str(sandbox["downloads"] / "a.pdf")), narrow)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.OUTSIDE_FROZEN_CAPABILITY",)


def test_path_outside_grant_is_denied(caps, sandbox):
    v = decide(call("fs.read", path=str(sandbox["documents"] / "secret.docx")), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.OUT_OF_SCOPE",)


def test_traversal_is_denied(caps, sandbox):
    sneaky = sandbox["downloads"] / ".." / "Documents" / "secret.docx"
    v = decide(call("fs.read", path=str(sneaky)), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.OUT_OF_SCOPE",)


def test_second_path_argument_is_also_checked(caps, sandbox):
    v = decide(call("fs.move",
                    src=str(sandbox["downloads"] / "a.pdf"),
                    dst=str(sandbox["documents"] / "a.pdf")), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.OUT_OF_SCOPE",)


# ---------- allow / confirm ----------

def test_reversible_file_read_runs_immediately(caps, sandbox):
    v = decide(call("fs.read", path=str(sandbox["downloads"] / "a.pdf")), caps)
    assert v.decision is Decision.ALLOW
    assert (v.rev, v.risk) == (Rev.REVERSIBLE, Risk.LOW)


def test_delete_of_one_file_is_reversible_and_allowed(caps, sandbox):
    v = decide(call("fs.delete", path=str(sandbox["downloads"] / "a.pdf")), caps)
    assert v.decision is Decision.ALLOW
    assert v.rev is Rev.REVERSIBLE


def test_delete_of_a_directory_escalates(caps, sandbox):
    v = decide(call("fs.delete", path=str(sandbox["downloads"] / "sub"),
                    recursive=True), caps)
    assert v.risk is Risk.HIGH
    assert v.decision is Decision.ALLOW      # still reversible: trash-backed


def test_compensable_action_asks_once(caps, sandbox):
    v = decide(call("git.commit", repo=str(sandbox["downloads"]),
                    message="wip"), caps)
    assert v.decision is Decision.CONFIRM_PLAN
    assert v.rev is Rev.COMPENSABLE


def test_irreversible_action_always_asks(caps):
    v = decide(call("mail.send", to="x@y.z", body="hi"), caps)
    assert v.decision is Decision.CONFIRM_STEP
    assert v.rev is Rev.IRREVERSIBLE


# ---------- the gate table itself ----------

def test_gate_table_is_total():
    from uaic.broker import GATE
    assert len(GATE) == len(Rev) * len(Risk), "every (rev, risk) pair needs a decision"


@pytest.mark.parametrize("rev", list(Rev))
def test_irreversible_is_never_silently_allowed(rev):
    from uaic.broker import GATE
    if rev is not Rev.IRREVERSIBLE:
        return
    for risk in Risk:
        assert GATE[(rev, risk)] is not Decision.ALLOW


# ---------- manifest-driven argument checking ----------

def test_undeclared_argument_is_denied(caps, sandbox):
    v = decide(call("fs.list", path=str(sandbox["downloads"]), sudo=True), caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.BAD_ARGS",)


def test_optional_argument_may_be_omitted(caps, sandbox):
    v = decide(call("fs.delete", path=str(sandbox["downloads"] / "a.pdf")), caps)
    assert v.decision is Decision.ALLOW


def test_step_budget_is_enforced(sandbox):
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"fs.list"}),
                         path_grants=(sandbox["downloads"],), max_steps=3)
    c = ToolCall(call_id="c99", plan_id="p1", tool="fs.list",
                 args={"path": str(sandbox["downloads"])}, seq=3)
    v = decide(c, caps)
    assert v.decision is Decision.DENY
    assert v.rule_ids == ("BRK.STEP_BUDGET_EXCEEDED",)


def test_denials_are_conservative(caps):
    """A call we cannot classify is treated as the worst case, never the best."""
    v = decide(call("nope.nope", path="x"), caps)
    assert (v.rev, v.risk) == (Rev.IRREVERSIBLE, Risk.CRITICAL)


def test_force_push_is_denied_outright(sandbox):
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.push_force"}),
                         path_grants=(sandbox["downloads"],))
    v = decide(call("git.push_force", repo=str(sandbox["downloads"]),
                    remote="origin"), caps)
    assert v.decision is Decision.DENY, "critical + irreversible must never run"
