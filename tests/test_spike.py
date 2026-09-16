from __future__ import annotations

from uaic.broker import CapabilitySet, decide
from uaic.contracts import Origin, ToolCall
from uaic.plugins import fs


def test_spike_end_to_end(sandbox):
    """A ToolCall travels broker -> plugin and comes back with real contents."""
    caps = CapabilitySet(plan_id="s1", tools=frozenset({"fs.list"}),
                         path_grants=(sandbox["downloads"],))
    c = ToolCall(call_id="c1", plan_id="s1", tool="fs.list",
                 args={"path": str(sandbox["downloads"])}, origin=Origin.USER)

    v = decide(c, caps)
    assert v.allowed

    r = fs.execute(c)
    assert r.ok
    assert set(r.payload) == {"a.pdf", "sub"}
    assert r.provenance.value == "untrusted"


def test_blocked_call_is_never_dispatched(sandbox):
    caps = CapabilitySet(plan_id="s1", tools=frozenset({"fs.list"}),
                         path_grants=(sandbox["downloads"],))
    c = ToolCall(call_id="c2", plan_id="s1", tool="fs.list",
                 args={"path": str(sandbox["documents"])})
    assert not decide(c, caps).allowed
