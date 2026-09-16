from __future__ import annotations

import pytest
from pydantic import ValidationError

from uaic.contracts import Decision, Origin, Provenance, Rev, Risk, ToolCall, Verdict


def test_toolcall_defaults():
    c = ToolCall(call_id="c1", plan_id="p1", tool="fs.list")
    assert c.origin is Origin.PLANNER
    assert c.seq == 0
    assert c.args == {}


def test_toolcall_is_frozen():
    c = ToolCall(call_id="c1", plan_id="p1", tool="fs.list")
    with pytest.raises(ValidationError):
        c.tool = "mail.send"


def test_verdict_helpers():
    allow = Verdict(decision=Decision.ALLOW, risk=Risk.LOW, rev=Rev.REVERSIBLE)
    ask = Verdict(decision=Decision.CONFIRM_STEP, risk=Risk.HIGH, rev=Rev.IRREVERSIBLE)
    deny = Verdict(decision=Decision.DENY, risk=Risk.CRITICAL, rev=Rev.IRREVERSIBLE)
    assert allow.allowed and not allow.needs_user
    assert ask.needs_user and not ask.allowed
    assert not deny.allowed and not deny.needs_user


def test_provenance_default_is_untrusted():
    from uaic.contracts import ToolResult
    r = ToolResult(call_id="c1", ok=True)
    assert r.provenance is Provenance.UNTRUSTED
