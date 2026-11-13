"""The mutation harness must never report a pass for a matrix it did not run.

It once printed "0 problem(s)" and exited 0 under an interpreter with no
pytest, having collected nothing. These run its main() with pytest stubbed
out, so they are fast and need no mutant to be applied.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "run_mutants", ROOT / "scripts" / "mutation" / "run_mutants.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def fake_pytest(monkeypatch, *, version_rc=0, collected=None):
    """Stand in for every pytest subprocess: --version, --collect-only, a run."""
    def run(argv, **kwargs):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, version_rc, "",
                                               "No module named pytest" if version_rc else "")
        if "--collect-only" in argv:
            path = argv[-1]
            ids = [n for n in (collected or []) if n.startswith(path + "::")]
            return subprocess.CompletedProcess(argv, 0 if ids else 5, "\n".join(ids), "")
        return subprocess.CompletedProcess(argv, 1, "", "")       # every mutant: RED
    monkeypatch.setattr(R.subprocess, "run", run)


def test_an_interpreter_without_pytest_fails_loudly(monkeypatch, capsys):
    fake_pytest(monkeypatch, version_rc=1)
    assert R.main() == 2
    out = capsys.readouterr()
    assert "No module named pytest" in out.err
    assert "0 problem(s)" not in out.out and "FAILED" in out.out


def test_collecting_nothing_fails_loudly(monkeypatch, capsys):
    fake_pytest(monkeypatch, collected=[])
    assert R.main() == 2
    assert "collecting tests/test_injection.py failed" in capsys.readouterr().err


@pytest.mark.parametrize("missing", [1, 10])
def test_fewer_pairs_than_expected_fails_loudly(monkeypatch, capsys, missing):
    # Every matrix file collects one mapped test, but far fewer pairs than pinned.
    collected = [f"{path}::test_{needle}" for path, needle, _ in R.MATRIX]
    planned = sum(len(R.mutants_for(n) or ()) for n in collected)
    fake_pytest(monkeypatch, collected=collected)
    monkeypatch.setattr(R, "EXPECTED_PAIRS", planned + missing)
    assert R.main() == 2
    assert f"maps {planned} test x mutant pair(s)" in capsys.readouterr().err


def test_the_pinned_count_is_what_a_full_run_checks_against(monkeypatch, capsys):
    collected = [f"{path}::test_{needle}" for path, needle, _ in R.MATRIX]
    planned = sum(len(R.mutants_for(n) or ()) for n in collected)
    fake_pytest(monkeypatch, collected=collected)
    monkeypatch.setattr(R, "EXPECTED_PAIRS", planned)
    assert R.main() == 1, "every pair ran; the =GREEN ones came back RED, so problems"
    assert f"{planned} pair(s) run" in capsys.readouterr().out
