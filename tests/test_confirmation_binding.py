"""Confirmation binding: a "yes" authorises the calls the user was shown.

The signature tests pin down what "materially different" means. The loop
tests check the two hazards that matter in practice: a retry must not
re-ask, and a call the user never saw must.
"""
from __future__ import annotations

import os

import pytest

from uaic.broker import default_registry
from uaic.contracts import Plan, PlanStep, ToolCall, ToolResult
from uaic.core import SessionMemory
from uaic.journal import ActionJournal, Status
from uaic.plugins import FileSystemPlugin
from uaic.runtime import AgentLoop, Executor, Outcome
from uaic.runtime.binding import signature

REG = default_registry()


def sig(tool, seq=1, call_id="c1", **args):
    return signature(ToolCall(call_id=call_id, plan_id="p", tool=tool, args=args, seq=seq),
                     REG)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "Repo" / "sub").mkdir(parents=True)
    (tmp_path / "Other").mkdir()
    return tmp_path / "Repo"


# ------------------------------------------------------------- the signature

def test_bookkeeping_fields_are_not_part_of_what_a_call_does(repo):
    """A retry changes call_id; a re-plan changes seq. Neither is a new action."""
    a = sig("git.commit", seq=1, call_id="c1", repo=str(repo), message="m")
    b = sig("git.commit", seq=7, call_id="c1-retry2", repo=str(repo), message="m")
    assert a == b


def test_paths_compare_as_the_broker_resolves_them_not_as_strings(repo):
    plain = sig("git.commit", repo=str(repo), message="m")
    for spelling in (repo.as_posix(), f"{repo}{os.sep}.", str(repo / "sub" / "..")):
        assert sig("git.commit", repo=spelling, message="m") == plain, spelling


@pytest.mark.skipif(os.name != "nt", reason="paths are case-insensitive only on Windows")
def test_path_case_is_not_a_difference_on_windows(repo):
    assert (sig("git.commit", repo=str(repo).upper(), message="m")
            == sig("git.commit", repo=str(repo), message="m"))


def test_traversal_to_a_different_target_is_a_different_call(repo):
    assert (sig("git.commit", repo=str(repo / ".." / "Other"), message="m")
            != sig("git.commit", repo=str(repo), message="m"))


@pytest.mark.parametrize("other", ["m2", "m ", "M"])
def test_any_change_to_a_string_argument_is_a_different_call(repo, other):
    """No whitespace or case folding: what the user read is what they approved."""
    assert (sig("git.commit", repo=str(repo), message=other)
            != sig("git.commit", repo=str(repo), message="m"))


def test_omitting_an_argument_equals_passing_its_default(repo):
    target = str(repo / "f.txt")
    assert sig("fs.delete", path=target) == sig("fs.delete", path=target, recursive=False)
    assert sig("fs.delete", path=target) != sig("fs.delete", path=target, recursive=True)


def test_an_unresolvable_path_matches_nothing_that_resolves(repo):
    assert sig("git.commit", repo="", message="m") != sig("git.commit", repo=str(repo),
                                                           message="m")


# ------------------------------------------------------------ through the loop

class Git:
    """Stands in for git.commit and fails the first `failures` calls."""

    def __init__(self, failures: int = 0) -> None:
        self.left = failures
        self.ran: list[dict] = []

    def execute(self, call: ToolCall) -> ToolResult:
        if self.left > 0:
            self.left -= 1
            return ToolResult(call_id=call.call_id, ok=False, error="index.lock exists")
        self.ran.append(dict(call.args))
        return ToolResult(call_id=call.call_id, ok=True, payload="committed")


class Planner:
    def __init__(self, *plans) -> None:
        self._plans = list(plans)

    def propose(self, prompt, plan_id, context=None):
        steps = self._plans.pop(0) if self._plans else []
        return Plan(plan_id=plan_id, prompt=prompt, steps=tuple(
            PlanStep(seq=i + 1, tool=t, args=a) for i, (t, a) in enumerate(steps)))


def run(tmp_path, git, *plans, confirm=None):
    asked: list[ToolCall] = []

    def user(call, verdict):
        asked.append(call)
        return True if confirm is None else confirm(call)

    journal = ActionJournal(tmp_path / "j.db")
    plugins = {"git": git.execute, "fs": FileSystemPlugin(tmp_path / "trash").execute}
    loop = AgentLoop(Planner(*plans), lambda a: Executor(journal, plugins, confirm=a),
                     approve=lambda plan: [tmp_path], confirm=user, memory=SessionMemory())
    report = loop.run("commit things", "p")
    journal.close()
    return report, asked


def commit(repo, message):
    return ("git.commit", {"repo": str(repo), "message": message})


def trip(tmp_path):
    return ("fs.read", {"path": str(tmp_path / "missing.txt")})     # fails: forces a re-plan


def test_a_retry_of_the_same_failed_call_is_not_asked_again(tmp_path, repo):
    git = Git(failures=2)

    report, asked = run(tmp_path, git, [commit(repo, "m")])

    assert report.outcome is Outcome.COMPLETED
    assert report.retries == 2
    assert len(asked) == 1
    assert git.ran == [{"repo": str(repo), "message": "m"}]


def test_an_approved_call_the_replan_keeps_is_covered_by_the_plan_confirmation(tmp_path, repo):
    """The user saw `b` in the preview and confirmed the plan. A re-plan that
    still contains `b` - spelled differently - has not added anything new."""
    git = Git()
    report, asked = run(tmp_path, git,
                        [commit(repo, "a"), trip(tmp_path), commit(repo, "b")],
                        [commit(repo.as_posix() + "/.", "b")])

    assert report.outcome is Outcome.COMPLETED and report.replans == 1
    assert [c.args["message"] for c in asked] == ["a"]
    assert [r["message"] for r in git.ran] == ["a", "b"]


def test_running_an_approved_call_a_second_time_asks_again(tmp_path, repo):
    """An authorisation is spent when its call succeeds. Twice is two actions."""
    git = Git()
    report, asked = run(tmp_path, git, [commit(repo, "a"), trip(tmp_path)],
                        [commit(repo, "a")])

    assert report.outcome is Outcome.COMPLETED
    assert [c.args["message"] for c in asked] == ["a", "a"]


def test_a_call_the_user_never_saw_is_asked_and_can_be_refused(tmp_path, repo):
    git = Git()
    report, asked = run(tmp_path, git, [commit(repo, "a"), trip(tmp_path)],
                        [commit(tmp_path / "Other", "a")],
                        confirm=lambda call: call.args["repo"] == str(repo))

    assert report.outcome is Outcome.DECLINED
    assert [c.args["repo"] for c in asked] == [str(repo), str(tmp_path / "Other")]
    assert git.ran == [{"repo": str(repo), "message": "a"}]
    assert report.steps[-1].status == Status.DECLINED


def test_step_level_confirmation_still_asks_on_every_retry(tmp_path, repo):
    """Deliberately stricter than plan level: a failed irreversible call may
    already have had its effect, so running it again is a new decision."""
    attempts = []

    def flaky_overwrite(call):
        attempts.append(call.call_id)
        return ToolResult(call_id=call.call_id, ok=len(attempts) == 3,
                          error=None if len(attempts) == 3 else "disk busy")

    target = repo / "f.txt"
    target.write_text("old\n", encoding="utf-8")
    asked: list[ToolCall] = []
    journal = ActionJournal(tmp_path / "j.db")
    loop = AgentLoop(Planner([("fs.overwrite", {"path": str(target), "content": "new"})]),
                     lambda a: Executor(journal, {"fs": flaky_overwrite}, confirm=a),
                     approve=lambda plan: [tmp_path],
                     confirm=lambda call, verdict: (asked.append(call), True)[1])
    report = loop.run("overwrite", "p")
    journal.close()

    assert report.outcome is Outcome.COMPLETED
    assert len(attempts) == 3 and len(asked) == 3
