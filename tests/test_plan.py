"""The Plan contract and the capability freeze."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from uaic.contracts import Origin, Plan, PlanStep
from uaic.core import PlanBuilder, ScriptedPlanner
from uaic.runtime import escalation_needed, freeze_plan


def a_plan(plan_id="p1") -> Plan:
    return (PlanBuilder(plan_id, "organise my downloads")
            .step("fs.list", "see what is there", path="/dl")
            .step("fs.move", "tidy it", src="/dl/a.pdf", dst="/dl/sorted/a.pdf")
            .needs_path("/dl")
            .build())


# ---------- the contract ----------

def test_steps_must_be_numbered_from_one(  ):
    with pytest.raises(ValidationError, match="numbered"):
        Plan(plan_id="p1", prompt="x",
             steps=(PlanStep(seq=2, tool="fs.list"),))


def test_step_numbers_start_at_one():
    with pytest.raises(ValidationError, match="start at 1"):
        PlanStep(seq=0, tool="fs.list")


def test_plan_is_frozen():
    plan = a_plan()
    with pytest.raises(ValidationError):
        plan.plan_id = "other"


def test_tools_is_the_distinct_set():
    plan = (PlanBuilder("p1", "x")
            .step("fs.move", src="a", dst="b")
            .step("fs.move", src="c", dst="d")
            .step("fs.list", path="/dl")
            .build())
    assert plan.tools() == frozenset({"fs.move", "fs.list"})


def test_to_calls_numbers_and_tags_them():
    calls = a_plan().to_calls()
    assert [c.seq for c in calls] == [1, 2]
    assert [c.call_id for c in calls] == ["p1-1", "p1-2"]
    assert all(c.origin is Origin.PLANNER for c in calls)
    assert all(c.plan_id == "p1" for c in calls)


def test_preview_shows_every_step_and_the_tool_set():
    text = a_plan().preview()
    assert "organise my downloads" in text
    assert "1. fs.list" in text and "2. fs.move" in text
    assert "tools required: fs.list, fs.move" in text


def test_scripted_planner_needs_no_model():
    planner = ScriptedPlanner([{"seq": 1, "tool": "fs.list", "args": {"path": "/dl"}}])
    plan = planner.propose("anything", "p9")
    assert plan.plan_id == "p9"
    assert plan.steps[0].tool == "fs.list"


# ---------- the freeze ----------

def test_freeze_fixes_exactly_the_plans_tools(tmp_path):
    caps = freeze_plan(a_plan(), [tmp_path])
    assert caps.tools == frozenset({"fs.list", "fs.move"})
    assert caps.plan_id == "p1"


def test_freeze_does_not_permit_a_tool_the_plan_never_used(tmp_path):
    """The whole contribution in one assertion."""
    caps = freeze_plan(a_plan(), [tmp_path])
    assert not caps.permits_tool("mail.send")
    assert not caps.permits_tool("fs.delete")


def test_frozen_set_cannot_be_mutated(tmp_path):
    caps = freeze_plan(a_plan(), [tmp_path])
    with pytest.raises(ValidationError):
        caps.tools = frozenset({"mail.send"})


def test_step_budget_leaves_a_little_headroom(tmp_path):
    caps = freeze_plan(a_plan(), [tmp_path])
    assert caps.max_steps == 2 + 2


def test_step_budget_can_be_pinned(tmp_path):
    assert freeze_plan(a_plan(), [tmp_path], max_steps=2).max_steps == 2


def test_escalation_reports_what_the_plan_wants_and_cannot_have(tmp_path):
    caps = freeze_plan(a_plan(), [tmp_path])
    wider = (PlanBuilder("p2", "and email it")
             .step("fs.list", path="/dl")
             .step("mail.send", to="x@y.z", body="hi")
             .build())
    assert escalation_needed(wider, caps) == frozenset({"mail.send"})


def test_nothing_to_escalate_for_the_plan_it_was_minted_from(tmp_path):
    plan = a_plan()
    assert escalation_needed(plan, freeze_plan(plan, [tmp_path])) == frozenset()


def test_an_empty_plan_grants_nothing(tmp_path):
    caps = freeze_plan(Plan(plan_id="p0", prompt="do nothing"), [tmp_path])
    assert caps.tools == frozenset()
    assert not caps.permits_tool("fs.list")
