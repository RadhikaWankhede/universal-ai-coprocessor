"""Running external programs, safely and identically for every plugin.

Three rules, and every plugin that shells out obeys all three:

1. Arguments are a LIST. `shell=False`, always. There is no string that a shell
   ever parses, so quoting, `;`, `&&` and backticks are not attack surface.
2. Every plan-supplied value that could be READ AS A FLAG is validated against
   a strict pattern before it is placed in that list, and may not begin with
   a hyphen. A list alone does not stop argument injection: a "branch name"
   of `--force` is a perfectly valid list element and git would read it as a
   flag.

   Two plan-supplied values are deliberately NOT pattern-checked. A commit
   message follows `-m` and a repository path follows `-C`; each flag
   consumes the next element as its value, so neither can be re-read as a
   flag. A commit message also cannot be pattern-restricted and stay useful;
   it is only required to be non-empty. The repository path is checked by
   other means: the broker scopes it, and git.py requires a `.git` entry
   inside it.
3. Every call has a timeout. A hung subprocess is a hung agent.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 60

# Deliberately narrow. Widening one of these patterns is a security change.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}(==[A-Za-z0-9][A-Za-z0-9._-]{0,20})?$")
BINARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,40}$")


class ProcessError(Exception):
    """The program could not be run, or was refused before being run."""


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def text(self) -> str:
        return (self.stdout or self.stderr).strip()


def checked(value: object, pattern: re.Pattern[str], label: str) -> str:
    """Validate one argument that will become an element of an argv list."""
    if not isinstance(value, str) or not value:
        raise ProcessError(f"{label} must be a non-empty string")
    if value.startswith("-"):
        raise ProcessError(f"{label} may not start with '-' (would be read as a flag)")
    if not pattern.match(value):
        raise ProcessError(f"{label} {value!r} is not an accepted {label}")
    return value


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ProcessError(f"{name} is not installed or not on PATH")
    return path


# The default runner. Plugins accept a replacement so tests can assert on the
# exact argv without executing anything.
def run_argv(argv: list[str], cwd: Path | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> Completed:
    try:
        # argv is a list and shell is False: no shell ever parses this.
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcessError(f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise ProcessError(f"could not run {argv[0]}: {exc}") from exc
    return Completed(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
