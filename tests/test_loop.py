"""The agent loop: recovery, and the boundary it must not cross.

The test that matters most in this file is
`test_a_replan_that_wants_a_new_tool_stops_and_asks`. Re-planning is the point
at which an agent naturally tries to give itself what it now believes it needs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from uaic.contracts import Plan, PlanStep, ToolCall, ToolResult
from uaic.core import PlanBuilder, PlannerError, SessionMemory
from uaic.journal import ActionJournal
from uaic.plugins import FileSystemPlugin
from uaic.runtime import AgentLoop, Executor, Outcome


class FakePlanner:
    """Returns a queued plan per call, so a re-plan can differ from the first."""

    def __init__(self, *plans, raises: Exception | None = None) -> None:
        self._plans = list(plans)
        self._raises = raises
        self.prompts: list[str] = []

    def propose(self, prompt, plan_id, context=None):
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        plan = self._plans.pop(0) if self._plans else self._plans[-1]
        return plan.model_copy(update={"plan_id": plan_id, "prompt": prompt})


class FlakyFs:
    """Wraps the real filesystem plugin and fails the first N calls to one tool."""

    def __init__(self, plugin, tool: str, failures: int) -> None:
        self._plugin = plugin
        self._tool = tool
        self._left = failures
        self.calls: list[str] = []

    def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call.call_id)
        if call.tool == self._tool and self._left > 0:
            self._left -= 1
            return ToolResult(call_id=call.call_id, ok=False,
                              error="the file is locked by another process")
        return self._plugin.execute(call)


@pytest.fixture
def workspace(tmp_path):
    downloads = tmp_path / "Downloads"
    documents = tmp_path / "Documents"
    downloads.mkdir()
    documents.mkdir()
    (downloads / "a.pdf").write_text("one\n", encoding="utf-8")
    (downloads / "b.pdf").write_text("two\n", encoding="utf-8")
    return tmp_path, downloads, documents


def tidy_plan(downloads: Path, documents: Path) -> Plan:
    return (PlanBuilder("p", "tidy my downloads")
            .step("fs.list", path=str(downloads))
            .step("fs.move", src=str(downloads / "a.pdf"),
                  dst=str(documents / "a.pdf"))
            .needs_path(str(downloads))
            .needs_path(str(documents))
            .build())


def build(tmp_path, planner, *, plugin=None, approve=None, confirm=None, **kw):
    journal = ActionJournal(tmp_path / "j.db")
    fs = plugin or FileSystemPlugin(tmp_path / "trash")
    loop = AgentLoop(
        planner,
        lambda asker: Executor(journal, {"fs": fs.execute, "mail": _mail},
                               confirm=asker),
        approve=approve or (lambda plan: plan.requested_paths),
        confirm=confirm or (lambda call, verdict: True),
        memory=SessionMemory(),
        **kw)
    return loop, journal


def _mail(call: ToolCall) -> ToolResult:
    """Loaded and working, so a refusal is the broker's doing."""
    return ToolResult(call_id=call.call_id, ok=True, payload="sent")


# ------------------------------------------------------------------ happy path

def test_an_approved_plan_of_reversible_steps_completes_without_questions(workspace):
    tmp_path, downloads, documents = workspace
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)))

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.COMPLETED
    assert len(report.executed) == 2
    assert report.confirmations == 0
    assert (documents / "a.pdf").exists()
    journal.close()


def test_every_result_is_written_into_memory_as_untrusted(workspace):
    tmp_path, downloads, documents = workspace
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)))
    loop.run("tidy my downloads", "run-1")

    kinds = [(i.kind, i.trusted) for i in loop.memory.items]
    assert ("user", True) in kinds
    assert all(trusted is False for kind, trusted in kinds if kind == "tool_result")
    journal.close()


# ------------------------------------------------------------------- approval

def test_declining_the_plan_runs_nothing_at_all(workspace):
    tmp_path, downloads, documents = workspace
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)),
                          approve=lambda plan: None)

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.DECLINED
    assert report.steps == []
    assert (downloads / "a.pdf").exists()
    journal.close()


def test_granting_less_than_the_plan_asked_for_refuses_the_out_of_scope_step(workspace):
    """The plan requests two folders; the user grants one."""
    tmp_path, downloads, documents = workspace
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)),
                          approve=lambda plan: (downloads,))

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.REFUSED
    assert report.steps[-1].verdict.rule_ids[0] == "BRK.OUT_OF_SCOPE"
    assert not (documents / "a.pdf").exists()
    journal.close()


def test_a_plan_level_question_is_asked_once_not_per_step(tmp_path):
    """Compensable steps share one confirmation. Asking five times is how you
    train someone to stop reading the dialog."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("x\n", encoding="utf-8")

    plan = Plan(plan_id="p", prompt="overwrite three files", steps=tuple(
        PlanStep(seq=i + 1, tool="fs.copy",
                 args={"src": str(repo / "f.txt"), "dst": str(repo / f"c{i}.txt")})
        for i in range(3)), requested_paths=(str(repo),))

    asked = []
    loop, journal = build(tmp_path, FakePlanner(plan),
                          confirm=lambda call, verdict: (asked.append(call.tool),
                                                         True)[1])
    report = loop.run("copy things", "run-1")
    assert report.outcome is Outcome.COMPLETED
    assert len(asked) <= 1          # fs.copy is reversible, so ideally zero
    journal.close()


# -------------------------------------------------------------------- recovery

def test_a_failing_step_is_retried_and_then_succeeds(workspace):
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=1)
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)),
                          plugin=flaky)

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.COMPLETED
    assert report.retries == 1
    assert (documents / "a.pdf").exists()
    journal.close()


def test_retries_are_bounded_and_then_the_loop_gives_up(workspace):
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=99)
    plan = tidy_plan(downloads, documents)
    loop, journal = build(tmp_path, FakePlanner(plan, plan), plugin=flaky,
                          max_retries=2, max_replans=0)

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.EXHAUSTED
    assert report.retries == 2
    assert "failed after 2 retries" in report.message
    journal.close()


def test_a_persistent_failure_triggers_exactly_one_replan(workspace):
    """The recovery plan stays INSIDE the frozen tool set, so it just runs."""
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=3)
    recovery = (PlanBuilder("p2", "move the other one instead")
                .step("fs.move", src=str(downloads / "b.pdf"),
                      dst=str(documents / "b.pdf"))
                .build())

    planner = FakePlanner(tidy_plan(downloads, documents), recovery)
    loop, journal = build(tmp_path, planner, plugin=flaky,
                          max_retries=2, max_replans=1)

    report = loop.run("tidy my downloads", "run-1")

    assert report.replans == 1
    assert report.outcome is Outcome.COMPLETED
    assert (documents / "b.pdf").exists()
    # the re-plan was asked with the failure in the prompt
    assert "failed" in planner.prompts[-1]
    journal.close()


def test_even_a_harmless_new_tool_in_a_replan_needs_approval(workspace):
    """Found by getting this test wrong first: the recovery plan reached for
    fs.copy, which is reversible, low risk, and touches only granted folders -
    and it was still refused, because it was not in the approved set. The
    freeze is about what the user agreed to, not about what is dangerous."""
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=99)
    recovery = (PlanBuilder("p2", "copy instead")
                .step("fs.copy", src=str(downloads / "b.pdf"),
                      dst=str(documents / "b.pdf"))
                .build())
    loop, journal = build(tmp_path,
                          FakePlanner(tidy_plan(downloads, documents), recovery),
                          plugin=flaky)

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.NEEDS_APPROVAL
    assert report.escalation == {"fs.copy"}
    assert not (documents / "b.pdf").exists()
    journal.close()


def test_the_replan_is_journalled_under_the_same_plan_id(workspace):
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=3)
    recovery = (PlanBuilder("p2", "move the other one")
                .step("fs.move", src=str(downloads / "b.pdf"),
                      dst=str(documents / "b.pdf"))
                .build())
    loop, journal = build(tmp_path,
                          FakePlanner(tidy_plan(downloads, documents), recovery),
                          plugin=flaky)
    loop.run("tidy my downloads", "run-1")

    rows = journal.rows("run-1")
    assert rows                      # one story, not two
    assert {r.plan_id for r in rows} == {"run-1"}
    journal.close()


# ---------------------------------------------- the boundary that must not move

def test_a_replan_that_wants_a_new_tool_stops_and_asks(workspace):
    """The heart of the project. The revised plan decides it needs to email
    someone. mail.send was never approved, a working mail plugin IS loaded,
    and the loop refuses to widen its own capability set."""
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=99)
    escalating = (PlanBuilder("p2", "ask for help")
                  .step("mail.send", to="admin@example.invalid", subject="s",
                        body="the move failed")
                  .build())

    loop, journal = build(tmp_path,
                          FakePlanner(tidy_plan(downloads, documents), escalating),
                          plugin=flaky)

    report = loop.run("tidy my downloads", "run-1")

    assert report.outcome is Outcome.NEEDS_APPROVAL
    assert report.escalation == {"mail.send"}
    assert report.caps.tools == {"fs.list", "fs.move"}        # unchanged
    assert not any(s.call.tool == "mail.send" for s in report.steps)
    journal.close()


def test_the_escalation_is_recorded_in_memory_for_the_user_to_see(workspace):
    tmp_path, downloads, documents = workspace
    flaky = FlakyFs(FileSystemPlugin(tmp_path / "trash"), "fs.move", failures=99)
    escalating = (PlanBuilder("p2", "ask for help")
                  .step("mail.send", to="a@b.invalid", subject="s", body="x")
                  .build())
    loop, journal = build(tmp_path,
                          FakePlanner(tidy_plan(downloads, documents), escalating),
                          plugin=flaky)
    loop.run("tidy my downloads", "run-1")

    assert any("escalation required" in i.text for i in loop.memory.items)
    journal.close()


def test_a_refusal_is_never_retried(workspace):
    """Retrying a denial would be a retry loop against the security layer."""
    tmp_path, downloads, _ = workspace
    outside = (PlanBuilder("p", "read something private")
               .step("fs.read", path=str(tmp_path / "Private" / "secret.txt"))
               .needs_path(str(downloads))
               .build())
    loop, journal = build(tmp_path, FakePlanner(outside),
                          approve=lambda plan: (downloads,))

    report = loop.run("read it", "run-1")

    assert report.outcome is Outcome.REFUSED
    assert report.retries == 0
    assert len(report.steps) == 1
    journal.close()


def test_declining_a_step_level_confirmation_stops_the_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    plan = (PlanBuilder("p", "overwrite it")
            .step("fs.overwrite", path=str(repo / "f.txt"), content="new")
            .needs_path(str(repo))
            .build())

    loop, journal = build(tmp_path, FakePlanner(plan),
                          confirm=lambda call, verdict: False)
    report = loop.run("overwrite it", "run-1")

    assert report.outcome is Outcome.DECLINED
    assert report.confirmations == 1
    assert (repo / "f.txt").read_text() == "x\n"        # untouched
    journal.close()


# ------------------------------------------------------------------- planning

def test_a_planner_that_produces_nothing_usable_is_reported_not_crashed(tmp_path):
    loop, journal = build(tmp_path, FakePlanner(
        raises=PlannerError("the model proposed no actions")))

    report = loop.run("do something", "run-1")

    assert report.outcome is Outcome.PLANNING_FAILED
    assert "no actions" in report.message
    assert report.steps == []
    journal.close()


def test_the_failure_is_remembered_so_the_next_turn_has_the_context(tmp_path):
    loop, journal = build(tmp_path, FakePlanner(raises=PlannerError("boom")))
    loop.run("do something", "run-1")
    assert any("planning failed" in i.text for i in loop.memory.items)
    journal.close()


def test_the_report_describes_itself_in_one_line(workspace):
    tmp_path, downloads, documents = workspace
    loop, journal = build(tmp_path, FakePlanner(tidy_plan(downloads, documents)))
    report = loop.run("tidy my downloads", "run-1")
    assert report.describe().startswith("completed: 2/2")
    journal.close()


# --------------------------------------------------------------- step budget

def test_the_budget_is_the_loops_own_declared_worst_case(workspace):
    """A retry reuses its step number; a re-plan continues the sequence. The
    budget has to cover the second case or legitimate recovery is refused as
    a runaway - which is exactly how this was found."""
    tmp_path, downloads, documents = workspace
    plan = tidy_plan(downloads, documents)
    loop, journal = build(tmp_path, FakePlanner(plan), max_replans=1)
    report = loop.run("tidy my downloads", "run-1")

    assert report.caps.max_steps == len(plan.steps) * 2 + 2
    journal.close()


def test_a_loop_with_no_replans_gets_a_tight_budget(workspace):
    tmp_path, downloads, documents = workspace
    plan = tidy_plan(downloads, documents)
    loop, journal = build(tmp_path, FakePlanner(plan), max_replans=0)
    report = loop.run("tidy my downloads", "run-1")

    assert report.caps.max_steps == len(plan.steps) + 2
    assert report.outcome is Outcome.COMPLETED
    journal.close()
