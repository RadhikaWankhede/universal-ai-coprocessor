"""Terminal plugin — a closed list of named operations.

There is no method here that accepts a command string, and adding one would
undo the project. Every shell capability is a named tool with typed arguments,
declared and classified in terminal.yaml before it exists.

The interpreter is always `sys.executable`, never the string "python". On a
machine with several Pythons on PATH - which is the normal case on Windows -
"python" is ambiguous, and an agent installing a package into the wrong
interpreter is a failure that looks like a bug for hours.
"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from uaic.contracts import Origin, Provenance, ToolCall, ToolResult
from uaic.plugins.process import (
    BINARY_RE,
    PACKAGE_RE,
    Completed,
    ProcessError,
    checked,
    require_binary,
    run_argv,
)

Runner = Callable[[list[str], Path | None], Completed]


class TerminalPlugin:
    name = "terminal"

    def __init__(self, *, runner: Runner | None = None, timeout: int = 120) -> None:
        # `runner` exists so tests can assert on the exact argv without any
        # package actually being installed on the machine running the tests.
        self._runner = runner
        self._timeout = timeout

    # ------------------------------------------------------------------ plumbing

    def _run(self, argv: list[str], cwd: Path | None = None) -> Completed:
        return (self._runner(argv, cwd) if self._runner
                else run_argv(argv, cwd, self._timeout))

    def _pip(self, *args: str) -> Completed:
        return self._run([sys.executable, "-m", "pip", "--disable-pip-version-check",
                          "--no-input", *args])

    def _inverse(self, call: ToolCall, tool: str, **args) -> ToolCall:
        return ToolCall(call_id=f"undo-{call.call_id}", plan_id=call.plan_id,
                        tool=tool, args=args, seq=call.seq, origin=Origin.UNDO)

    @staticmethod
    def _repo(call: ToolCall) -> Path:
        repo = Path(call.args["repo"])
        if not repo.is_dir():
            raise ProcessError(f"{repo} is not a directory")
        return repo

    # ------------------------------------------------------------------ dispatch

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        handler = {
            "terminal.python_version": self._python_version,
            "terminal.which": self._which,
            "terminal.pip_list": self._pip_list,
            "terminal.pip_install": self._pip_install,
            "terminal.pip_uninstall": self._pip_uninstall,
            "terminal.run_tests": self._run_tests,
            "terminal.run_linter": self._run_linter,
        }.get(call.tool)

        if handler is None:
            return self._fail(call, f"terminal plugin does not implement {call.tool}",
                              started)
        try:
            payload, inverse = handler(call)
        except (ProcessError, KeyError, OSError) as exc:
            return self._fail(call, f"{type(exc).__name__}: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            payload=payload,
            inverse=inverse,
            provenance=Provenance.UNTRUSTED,   # program output is never trusted
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _fail(call: ToolCall, error: str, started: float) -> ToolResult:
        return ToolResult(call_id=call.call_id, ok=False, error=error,
                          duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ handlers

    def _python_version(self, call: ToolCall):
        done = self._run([sys.executable, "--version"])
        return done.text(), None

    def _which(self, call: ToolCall):
        name = checked(call.args["binary"], BINARY_RE, "binary")
        try:
            return {"binary": name, "path": require_binary(name)}, None
        except ProcessError:
            return {"binary": name, "path": None}, None

    def _pip_list(self, call: ToolCall):
        done = self._pip("list", "--format=freeze")
        if not done.ok:
            raise ProcessError(f"pip list failed: {done.text()}")
        return sorted(ln for ln in done.stdout.splitlines() if ln.strip()), None

    def _pip_install(self, call: ToolCall):
        package = checked(call.args["package"], PACKAGE_RE, "package")
        done = self._pip("install", package)
        if not done.ok:
            raise ProcessError(f"pip install {package} failed: {done.text()}")
        bare = package.split("==", 1)[0]
        return ({"installed": package},
                self._inverse(call, "terminal.pip_uninstall", package=bare))

    def _pip_uninstall(self, call: ToolCall):
        package = checked(call.args["package"], PACKAGE_RE, "package")
        done = self._pip("uninstall", "--yes", package)
        if not done.ok:
            raise ProcessError(f"pip uninstall {package} failed: {done.text()}")
        # The inverse reinstalls by name only: the exact version that was
        # present is not recoverable after removal, and claiming otherwise
        # would be a lie in the journal.
        return ({"uninstalled": package},
                self._inverse(call, "terminal.pip_install",
                              package=package.split("==", 1)[0]))

    def _run_tests(self, call: ToolCall):
        repo = self._repo(call)
        done = self._run([sys.executable, "-m", "pytest", "-q"], repo)
        # A failing suite is a real answer, not a plugin error: the tool did
        # what it was asked. Only being unable to run pytest at all is an error.
        return {"returncode": done.returncode, "passed": done.ok,
                "output": done.text()[-4000:]}, None

    def _run_linter(self, call: ToolCall):
        repo = self._repo(call)
        done = self._run([sys.executable, "-m", "ruff", "check", "."], repo)
        return {"returncode": done.returncode, "clean": done.ok,
                "output": done.text()[-4000:]}, None
