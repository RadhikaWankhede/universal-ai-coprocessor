"""Git plugin: real repositories in a temp folder, no network, no remote."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from uaic.broker import CapabilitySet, decide
from uaic.contracts import Decision, Rev, Risk, ToolCall
from uaic.plugins import GitPlugin

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")


def _git(repo, *args):
    subprocess.run([shutil.which("git"), "-C", str(repo), *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    # local identity only: never touch the developer's global git config
    _git(r, "config", "user.email", "test@example.invalid")
    _git(r, "config", "user.name", "Test")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "initial")
    return r


def call(tool, seq=1, **args):
    return ToolCall(call_id=f"c-{seq}", plan_id="p1", tool=tool, args=args, seq=seq)


@pytest.fixture
def plugin():
    return GitPlugin()


# ----------------------------------------------------------------------- status

def test_status_reports_a_clean_tree(plugin, repo):
    out = plugin.execute(call("git.status", repo=str(repo)))
    assert out.ok
    assert out.payload["clean"] is True


def test_status_reports_a_dirty_tree(plugin, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    out = plugin.execute(call("git.status", repo=str(repo)))
    assert out.payload["clean"] is False


def test_a_folder_that_is_not_a_repo_is_an_error_not_a_crash(plugin, tmp_path):
    out = plugin.execute(call("git.status", repo=str(tmp_path)))
    assert not out.ok
    assert "not a git repository" in out.error


# ----------------------------------------------------------------------- commit

def test_commit_returns_the_sha_and_an_inverse(plugin, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    out = plugin.execute(call("git.commit", repo=str(repo), message="add b"))
    assert out.ok
    assert len(out.payload["sha"]) == 40
    assert out.inverse is not None
    assert out.inverse.tool == "git.revert"
    assert out.inverse.args["sha"] == out.payload["sha"]


def test_an_empty_commit_message_is_refused_before_git_can_open_an_editor(plugin, repo):
    out = plugin.execute(call("git.commit", repo=str(repo), message="   "))
    assert not out.ok
    assert "non-empty" in out.error


def test_the_recorded_inverse_actually_reverses_the_commit(plugin, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    committed = plugin.execute(call("git.commit", repo=str(repo), message="add b"))
    assert (repo / "b.txt").exists()

    undone = plugin.execute(committed.inverse)
    assert undone.ok
    assert not (repo / "b.txt").exists()      # the revert commit removed it


def test_revert_records_no_inverse_so_undo_does_not_oscillate(plugin, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    committed = plugin.execute(call("git.commit", repo=str(repo), message="add b"))
    undone = plugin.execute(committed.inverse)
    assert undone.inverse is None


# ---------------------------------------------------------------------- branches

def test_branch_creates_and_its_inverse_deletes(plugin, repo):
    made = plugin.execute(call("git.branch", repo=str(repo), name="feature/login"))
    assert made.ok
    assert "feature/login" in _branches(repo)

    removed = plugin.execute(made.inverse)
    assert removed.ok
    assert "feature/login" not in _branches(repo)


def test_undoing_a_branch_deletion_restores_the_commit_it_pointed_at(plugin, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "second")
    head = _rev(repo, "HEAD")
    _git(repo, "branch", "keep", head)
    _git(repo, "checkout", "-q", "HEAD~1")        # move HEAD away from the tip

    deleted = plugin.execute(call("git.branch_delete", repo=str(repo), name="keep"))
    assert deleted.ok
    assert deleted.payload["was_at"] == head

    restored = plugin.execute(deleted.inverse)
    assert restored.ok
    # the branch is back at its original commit, not at the current HEAD
    assert _rev(repo, "keep") == head


def test_a_branch_name_that_is_really_a_flag_is_refused(plugin, repo):
    out = plugin.execute(call("git.branch", repo=str(repo), name="--force"))
    assert not out.ok
    assert "may not start with '-'" in out.error


def test_a_branch_name_with_shell_metacharacters_is_refused(plugin, repo):
    out = plugin.execute(call("git.branch", repo=str(repo), name="x; rm -rf /"))
    assert not out.ok


def test_a_malformed_sha_is_refused(plugin, repo):
    out = plugin.execute(call("git.revert", repo=str(repo), sha="HEAD; echo hi"))
    assert not out.ok


# ------------------------------------------------------------------ push_force

def test_push_force_is_denied_by_the_gate_before_any_dispatch():
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.push_force"}),
                         path_grants=())
    verdict = decide(call("git.push_force", repo=".", remote="origin"), caps)
    assert verdict.decision is Decision.DENY
    assert verdict.risk is Risk.CRITICAL
    assert verdict.rev is Rev.IRREVERSIBLE


def test_push_force_has_deliberately_no_implementation(plugin, repo):
    out = plugin.execute(call("git.push_force", repo=str(repo), remote="origin"))
    assert not out.ok
    assert "does not implement" in out.error


# ---------------------------------------------------------------------- helpers

def _branches(repo):
    done = subprocess.run([shutil.which("git"), "-C", str(repo), "branch",
                           "--format=%(refname:short)"],
                          check=True, capture_output=True, text=True)
    return done.stdout.split()


def _rev(repo, ref):
    done = subprocess.run([shutil.which("git"), "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True)
    return done.stdout.strip()


# ------------------------------------------- through the real broker + journal

def test_a_commit_costs_one_plan_level_confirmation(repo, tmp_path):
    """fs.move is free; committing is not. That difference is the taxonomy
    doing its job: a commit is compensable, not reversible."""
    from uaic.broker import CapabilitySet
    from uaic.journal import ActionJournal
    from uaic.runtime import Executor

    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    asked = []
    journal = ActionJournal(tmp_path / "j.db")
    executor = Executor(journal, {"git": GitPlugin().execute},
                        confirm=lambda c, v: (asked.append(c.tool), True)[1])
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.status", "git.commit"}),
                         path_grants=(repo,))

    report = executor.run_plan([
        call("git.status", seq=1, repo=str(repo)),
        call("git.commit", seq=2, repo=str(repo), message="add b"),
    ], caps)

    assert len(report.executed) == 2
    assert report.confirmations == 1          # the commit, not the status
    assert asked == ["git.commit"]
    journal.close()


def test_declining_the_confirmation_means_no_commit_is_made(repo, tmp_path):
    from uaic.broker import CapabilitySet
    from uaic.journal import ActionJournal
    from uaic.runtime import Executor

    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    before = _rev(repo, "HEAD")

    journal = ActionJournal(tmp_path / "j.db")
    executor = Executor(journal, {"git": GitPlugin().execute},
                        confirm=lambda c, v: False)
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.commit"}),
                         path_grants=(repo,))

    report = executor.run_plan([call("git.commit", repo=str(repo), message="add b")],
                               caps)
    assert len(report.refused) == 1
    assert _rev(repo, "HEAD") == before       # history untouched
    journal.close()


def test_undo_reverses_a_committed_plan(repo, tmp_path):
    from uaic.broker import CapabilitySet
    from uaic.journal import ActionJournal, UndoEngine
    from uaic.runtime import Executor

    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    plugin = GitPlugin()
    journal = ActionJournal(tmp_path / "j.db")
    executor = Executor(journal, {"git": plugin.execute}, confirm=lambda c, v: True)
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.commit"}),
                         path_grants=(repo,))
    executor.run_plan([call("git.commit", repo=str(repo), message="add b")], caps)
    assert (repo / "b.txt").exists()

    report = UndoEngine(journal, plugin.execute).undo_plan("p1")
    assert report.complete
    assert not (repo / "b.txt").exists()
    journal.close()


def test_a_git_tool_outside_the_frozen_set_is_refused(repo, tmp_path):
    from uaic.broker import CapabilitySet
    from uaic.journal import ActionJournal
    from uaic.runtime import Executor

    journal = ActionJournal(tmp_path / "j.db")
    executor = Executor(journal, {"git": GitPlugin().execute})
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"git.status"}),
                         path_grants=(repo,))

    outcome = executor.run_one(call("git.commit", repo=str(repo), message="x"), caps)
    assert outcome.verdict.rule_ids[0] == "BRK.OUTSIDE_FROZEN_CAPABILITY"
    assert not outcome.ran
    journal.close()
