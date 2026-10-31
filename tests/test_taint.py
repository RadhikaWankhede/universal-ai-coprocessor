"""Taint propagation and gate escalation.

A plan is tainted when untrusted content was in the memory its planner was
given. The runtime sets the taint, never the planner. A call that is tainted
AND not one of the approved calls moves up one tier of the gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from uaic.broker import CapabilitySet
from uaic.contracts import Decision, Plan, PlanStep, Rev, Risk, ToolCall, Verdict, parse_source
from uaic.core import SessionMemory
from uaic.journal import ActionJournal
from uaic.plugins import FileSystemPlugin
from uaic.runtime import AgentLoop, Executor, Outcome
from uaic.runtime.taint import ESCALATE, escalate


class Planner:
    """Returns queued plans; records what it was handed. May lie about taint."""

    def __init__(self, *plans, claims: dict | None = None) -> None:
        self._plans = list(plans)
        self._claims = claims or {}
        self.proposed: list[Plan] = []

    def propose(self, prompt, plan_id, context=None):
        steps = self._plans.pop(0) if self._plans else []
        plan = Plan(plan_id=plan_id, prompt=prompt, **self._claims, steps=tuple(
            PlanStep(seq=i + 1, tool=t, args=a) for i, (t, a) in enumerate(steps)))
        self.proposed.append(plan)
        return plan


@pytest.fixture
def box(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "report.txt").write_text("quarterly figures\n", encoding="utf-8")
    (inbox / "a.txt").write_text("a\n", encoding="utf-8")
    return inbox


def loop_for(tmp_path, planner, *, memory=None, confirm=None):
    journal = ActionJournal(tmp_path / "j.db")
    plugins = {"fs": FileSystemPlugin(tmp_path / "trash").execute}
    loop = AgentLoop(planner, lambda a: Executor(journal, plugins, confirm=a),
                     approve=lambda plan: [tmp_path],
                     confirm=confirm or (lambda call, verdict: True),
                     memory=memory or SessionMemory())
    return loop, journal


def read(path: Path):
    return ("fs.read", {"path": str(path)})


# ----------------------------------------------------------------- propagation

def test_a_first_plan_in_a_fresh_session_is_not_tainted(tmp_path, box):
    planner = Planner([read(box / "report.txt")])
    loop, journal = loop_for(tmp_path, planner)

    report = loop.run("read the report", "p")

    assert report.outcome is Outcome.COMPLETED
    assert not report.plan.tainted and report.plan.taint_sources == ()
    assert not report.steps[0].call.tainted
    journal.close()


def test_a_replan_after_a_read_is_tainted_with_every_source_in_order(tmp_path, box):
    planner = Planner([read(box / "report.txt"), read(box / "missing.txt")],
                      [read(box / "a.txt")])
    loop, journal = loop_for(tmp_path, planner)

    report = loop.run("read the report", "p")

    assert report.replans == 1
    recovery = report.steps[-1].call
    assert recovery.tainted
    assert recovery.taint_sources == (f"{(box / 'report.txt').as_uri()}#fs.read",
                                      f"{(box / 'missing.txt').as_uri()}#fs.read")
    journal.close()


def test_a_failure_note_alone_taints_the_replan(tmp_path, box):
    """The broad rule: error text quotes what failed, so it is untrusted input."""
    planner = Planner([read(box / "missing.txt")], [read(box / "a.txt")])
    loop, journal = loop_for(tmp_path, planner)

    report = loop.run("read it", "p")

    assert [i.kind for i in loop.memory.items if not i.trusted][:1] == ["note"]
    assert report.steps[-1].call.tainted
    assert report.steps[-1].call.taint_sources == (f"{(box / 'missing.txt').as_uri()}#fs.read",)
    journal.close()


@pytest.mark.parametrize("claims", [{"tainted": False},
                                    {"tainted": True, "taint_sources": ("forged:x",)}])
def test_a_planner_cannot_set_its_own_taint_in_either_direction(tmp_path, box, claims):
    planner = Planner([read(box / "missing.txt")], [read(box / "a.txt")], claims=claims)
    loop, journal = loop_for(tmp_path, planner)

    report = loop.run("read it", "p")

    assert not report.plan.tainted and report.plan.taint_sources == ()   # first plan: clean
    assert report.steps[-1].call.tainted
    assert "forged:x" not in report.steps[-1].call.taint_sources
    journal.close()


def test_a_retry_keeps_the_taint_of_the_call_it_retries(tmp_path, box):
    planner = Planner([read(box / "report.txt"), read(box / "missing.txt")],
                      [read(box / "missing.txt")])
    loop, journal = loop_for(tmp_path, planner)

    report = loop.run("read it", "p")

    replanned = [s.call for s in report.steps if "replan" in s.call.call_id]
    assert len(replanned) == 3                                  # the call and two retries
    assert len({c.taint_sources for c in replanned}) == 1 and replanned[0].tainted
    journal.close()


def test_the_next_runs_first_plan_carries_the_previous_runs_reads(tmp_path, box):
    memory = SessionMemory()
    loop, journal = loop_for(tmp_path, Planner([read(box / "report.txt")], [read(box / "a.txt")]),
                             memory=memory)
    loop.run("read the report", "p1")
    second = loop.run("now the other one", "p2")

    assert second.plan.tainted
    assert second.plan.taint_sources == (f"{(box / 'report.txt').as_uri()}#fs.read",)
    # an approved call is never escalated, tainted or not
    assert second.steps[0].verdict.decision is Decision.ALLOW
    journal.close()


# ------------------------------------------------------------------ escalation

def verdict(decision: Decision) -> Verdict:
    return Verdict(decision=decision, risk=Risk.LOW, rev=Rev.REVERSIBLE,
                   reasons=("r",), rule_ids=("BRK.GATE",))


TAINTED = ToolCall(call_id="c", plan_id="p", tool="fs.read", args={}, tainted=True,
                   taint_sources=("file:///C:/inbox/report.pdf#doc.extract_text",))


@pytest.mark.parametrize(("before", "after"), [
    (Decision.ALLOW, Decision.CONFIRM_PLAN),
    (Decision.CONFIRM_PLAN, Decision.CONFIRM_STEP),
    (Decision.CONFIRM_STEP, Decision.CONFIRM_STEP),
    (Decision.DENY, Decision.DENY),
])
def test_escalation_moves_exactly_one_tier_and_confirm_step_does_not_become_deny(before, after):
    out = escalate(verdict(before), TAINTED)
    assert out.decision is after
    if before is after:
        assert out == verdict(before)                         # untouched, not re-labelled
    else:
        assert out.rule_ids == ("BRK.GATE", "RT.TAINT_ESCALATION")
        assert "report.pdf" in out.reasons[-1]


def test_the_escalation_table_is_literal_data_covering_every_decision():
    assert set(ESCALATE) == set(Decision)


@pytest.fixture
def ex(tmp_path, box):
    journal = ActionJournal(tmp_path / "j.db")
    yield Executor(journal, {"fs": FileSystemPlugin(tmp_path / "trash").execute},
                   confirm=lambda c, v: False), box
    journal.close()


def caps(box):
    return CapabilitySet(plan_id="p", tools=frozenset({"fs.read"}), path_grants=(box,))


def call(box, *, tainted, name="report.txt"):
    return ToolCall(call_id="c", plan_id="p", tool="fs.read", seq=1,
                    args={"path": str(box / name)}, tainted=tainted,
                    taint_sources=("file:///x#fs.read",) if tainted else ())


def test_a_tainted_call_outside_the_approved_plan_is_escalated(ex):
    executor, box = ex
    ledger = executor.ledger_for([call(box, tainted=False, name="a.txt")])
    out = executor.run_one(call(box, tainted=True), caps(box), ledger)
    assert out.verdict.decision is Decision.CONFIRM_PLAN
    assert out.status == "declined"                        # the confirmer says no


def test_a_tainted_call_that_was_approved_is_not_escalated(ex):
    executor, box = ex
    ledger = executor.ledger_for([call(box, tainted=False)])
    out = executor.run_one(call(box, tainted=True), caps(box), ledger)
    assert out.verdict.decision is Decision.ALLOW
    assert out.status == "executed"


def test_an_untainted_call_outside_the_approved_plan_is_not_escalated(ex):
    executor, box = ex
    ledger = executor.ledger_for([call(box, tainted=False, name="a.txt")])
    out = executor.run_one(call(box, tainted=False), caps(box), ledger)
    assert out.verdict.decision is Decision.ALLOW


def test_without_a_ledger_nothing_counts_as_approved(ex):
    executor, box = ex
    out = executor.run_one(call(box, tainted=True), caps(box))
    assert out.verdict.decision is Decision.CONFIRM_PLAN


# --------------------------------------------------------------- preview data

def test_a_plans_sources_are_available_to_the_preview_as_data():
    plan = Plan(plan_id="p", prompt="x", tainted=True, taint_sources=(
        "file:///C:/Users/me/inbox/report.pdf#doc.extract_text",
        "tool:fs.list",
        "planner:p-r1",
    ))
    first, second, third = plan.sources_read()
    assert (first.kind, first.name, first.tool) == ("file", "report.pdf", "doc.extract_text")
    assert first.path == "C:/Users/me/inbox/report.pdf"
    assert (second.kind, second.name, second.tool) == ("tool", "fs.list", "fs.list")
    assert (third.kind, third.name) == ("planner", "p-r1")


def test_a_percent_encoded_source_decodes_to_the_real_name():
    """source_of() percent-encodes hostile characters; the preview gets them back
    as plain data, never as markup."""
    src = parse_source(Path("C:/inbox/my report\".pdf").as_uri() + "#doc.extract_text")
    assert src.name == 'my report".pdf'


def test_an_unrecognised_source_is_kept_rather_than_dropped():
    assert parse_source("unknown").kind == "unknown"
    assert parse_source("unknown").name == "unknown"
