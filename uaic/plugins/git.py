"""Git plugin.

Two things worth reading for:

`git.commit` is the project's first COMPENSABLE operation. It cannot be
un-happened — history is append-only — but it can be answered by a new commit
that reverses it. That is a different tier from `fs.move`, which can be put
back exactly, and it is why committing costs a plan-level confirmation while
moving a file costs nothing.

`git.push_force` is declared critical and irreversible, which the policy table
maps to DENY, so the broker refuses it before dispatch. This plugin therefore
has NO implementation for it. That is the opposite choice from the mail stub in
the demo, which is implemented on purpose to prove the broker is doing the
refusing - here the action would rewrite a remote history that is not ours to
lose, so an unreachable implementation is not worth having at all.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from uaic.contracts import Origin, Provenance, ToolCall, ToolResult
from uaic.plugins.process import (
    BRANCH_RE,
    SHA_RE,
    Completed,
    ProcessError,
    checked,
    require_binary,
    run_argv,
)

Runner = Callable[[list[str], Path | None], Completed]


class GitPlugin:
    name = "git"

    def __init__(self, *, runner: Runner | None = None, timeout: int = 60) -> None:
        self._runner = runner
        self._timeout = timeout

    # ------------------------------------------------------------------ plumbing

    def _git(self, repo: Path, *args: str) -> Completed:
        argv = [require_binary("git"), *args]
        done = (self._runner(argv, repo) if self._runner
                else run_argv(argv, repo, self._timeout))
        if not done.ok:
            raise ProcessError(f"git {' '.join(args)} failed: {done.text()}")
        return done

    @staticmethod
    def _repo(call: ToolCall) -> Path:
        repo = Path(call.args["repo"])
        if not (repo / ".git").exists():
            raise ProcessError(f"{repo} is not a git repository")
        return repo

    def _inverse(self, call: ToolCall, tool: str, **args) -> ToolCall:
        return ToolCall(call_id=f"undo-{call.call_id}", plan_id=call.plan_id,
                        tool=tool, args=args, seq=call.seq, origin=Origin.UNDO)

    # ------------------------------------------------------------------ dispatch

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        handler = {
            "git.status": self._status,
            "git.commit": self._commit,
            "git.branch": self._branch,
            "git.revert": self._revert,
            "git.branch_delete": self._branch_delete,
        }.get(call.tool)

        if handler is None:
            # git.push_force lands here by design; see the module docstring.
            return self._fail(call, f"git plugin does not implement {call.tool}", started)
        try:
            payload, inverse = handler(call)
        except (ProcessError, KeyError, OSError) as exc:
            return self._fail(call, f"{type(exc).__name__}: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            payload=payload,
            inverse=inverse,
            # Command output is data produced outside this system. It is never
            # trusted, whether the command mutated anything or not.
            provenance=Provenance.UNTRUSTED,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _fail(call: ToolCall, error: str, started: float) -> ToolResult:
        return ToolResult(call_id=call.call_id, ok=False, error=error,
                          duration_ms=int((time.perf_counter() - started) * 1000))

    # ------------------------------------------------------------------ handlers

    def _status(self, call: ToolCall):
        repo = self._repo(call)
        out = self._git(repo, "-C", str(repo), "status", "--porcelain=v1", "--branch")
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        return {"clean": len(lines) <= 1, "lines": lines}, None

    def _commit(self, call: ToolCall):
        repo = self._repo(call)
        message = call.args["message"]
        if not isinstance(message, str) or not message.strip():
            raise ProcessError("commit message must be a non-empty string")
        # -m takes the next argv element verbatim, so the message needs no
        # pattern - but it must not be empty, or git opens an editor and hangs.
        self._git(repo, "-C", str(repo), "commit", "-m", message)
        sha = self._git(repo, "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        return ({"sha": sha, "message": message},
                self._inverse(call, "git.revert", repo=str(repo), sha=sha))

    def _revert(self, call: ToolCall):
        repo = self._repo(call)
        sha = checked(call.args["sha"], SHA_RE, "sha")
        self._git(repo, "-C", str(repo), "revert", "--no-edit", sha)
        new_sha = self._git(repo, "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        # Reverting a revert is a further commit, not a return to the prior
        # state, so no inverse is recorded: undo stops rather than oscillating.
        return {"reverted": sha, "sha": new_sha}, None

    def _branch(self, call: ToolCall):
        repo = self._repo(call)
        name = checked(call.args["name"], BRANCH_RE, "branch name")
        start = call.args.get("start_point")
        args = ["branch", name]
        if start is not None:
            args.append(checked(start, SHA_RE, "start_point"))
        self._git(repo, "-C", str(repo), *args)
        return ({"branch": name, "start_point": start},
                self._inverse(call, "git.branch_delete", repo=str(repo), name=name))

    def _branch_delete(self, call: ToolCall):
        repo = self._repo(call)
        name = checked(call.args["name"], BRANCH_RE, "branch name")
        sha = self._git(repo, "-C", str(repo), "rev-parse", name).stdout.strip()
        self._git(repo, "-C", str(repo), "branch", "-D", name)
        # Recreating the branch needs the commit it pointed at, which is why we
        # read the sha BEFORE deleting. Afterwards the name no longer resolves.
        return ({"deleted": name, "was_at": sha},
                self._inverse(call, "git.branch", repo=str(repo), name=name,
                              start_point=sha))
