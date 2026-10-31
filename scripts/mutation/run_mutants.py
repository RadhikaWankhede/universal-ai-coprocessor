"""Check that every injection test can fail: weaken its defence, expect red.

    py scripts/mutation/run_mutants.py            # the whole matrix
    py scripts/mutation/run_mutants.py --show     # also print why each went red

Each test is run in a fresh pytest process with one mutant from mutants.py
applied. A test that stays GREEN with its defence removed is measuring
nothing. Exit status is 0 only if every mapped run went red and every test
in the injection suite is mapped.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SUITE = "tests/test_injection.py"

# (file, substring of the test id) -> the mutants that must turn it red.
# First match wins, so the more specific entries come first.
MATRIX: list[tuple[str, str, tuple[str, ...]]] = [
    (SUITE, "residual_poisoned_memory", ()),     # no code defence; see README
    (SUITE, "residual_injected_call_to_a_frozen_reversible", ("UNDO_OFF",)),
    (SUITE, "residual_injected_call_to_a_plan_confirmed_compensable", ("ASK_EVERY_STEP",)),
    (SUITE, "trusted_channel", ("KEYWORD_FILTER",)),
    (SUITE, "every_attack_target", ("PLUGINS_MISSING",)),
    (SUITE, "[loop-escalation]", ("ESCALATION_OFF",)),
    (SUITE, "does_not_exist__doc_extract__defeated_by_freeze_and_registry[broker-freeze]",
     ("UNDECLARED_OFF",)),
    (SUITE, "[broker-freeze]", ("FREEZE_OFF",)),
    (SUITE, "nonce_boundary", ("NONCE_FIXED",)),
    (SUITE, "forged_truncation", ("NONCE_FIXED",)),
    (SUITE, "outside_the_grant", ("SCOPE_OFF",)),
    (SUITE, "path_traversal", ("NO_RESOLVE",)),
    (SUITE, "cut_and_the_cut_is_announced", ("SILENT_TRUNC",)),
    (SUITE, "error_message__git_commit_stderr__defeated_by_untrusted_notes",
     ("NOTED_TRUSTED", "PROMPT_LEAK")),
    (SUITE, "trusted_memory_entry", ("OBSERVED_TRUSTED",)),
    (SUITE, "laundering_a_quote", ("OBSERVED_TRUSTED",)),
    (SUITE, "confirmation_is_not_required", ("GATE_ALLOW",)),
    ("tests/test_loop.py", "untrusted_error_text_never_reaches_the_planner",
     ("PROMPT_LEAK",)),
]


def collect(path: str) -> list[str]:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", path],
                         cwd=ROOT, capture_output=True, text=True, check=False).stdout
    return [line for line in out.split() if line.startswith(path + "::")]


def mutants_for(nodeid: str) -> tuple[str, ...] | None:
    for path, needle, mutants in MATRIX:
        if nodeid.startswith(path + "::") and needle in nodeid:
            return mutants
    return None


def run(nodeid: str, mutant: str) -> tuple[str, str]:
    env = dict(os.environ, MUTANT=mutant,
               PYTHONPATH=os.pathsep.join(filter(None, [str(HERE),
                                                        os.environ.get("PYTHONPATH")])))
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "mutants",
                           "-p", "no:cacheprovider", nodeid],
                          cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    verdict = {0: "GREEN", 1: "RED"}.get(proc.returncode, f"ERROR(rc={proc.returncode})")
    why = next((line for line in proc.stdout.splitlines() if line.startswith("E ")), "")
    return verdict, why


def main() -> int:
    show = "--show" in sys.argv
    nodeids = collect(SUITE) + [n for n in collect("tests/test_loop.py")
                                if mutants_for(n) is not None]
    problems = 0
    for nodeid in nodeids:
        name = nodeid.split("::", 1)[1]
        mutants = mutants_for(nodeid)
        if mutants is None:
            print(f"{'UNMAPPED':16} {'?':6} {name}")
            problems += 1
            continue
        if not mutants:
            print(f"{'(no mutant)':16} {'n/a':6} {name}")
            continue
        for mutant in mutants:
            verdict, why = run(nodeid, mutant)
            print(f"{mutant:16} {verdict:6} {name}")
            if show and why:
                print(f"{'':23}{why[:150]}")
            problems += verdict != "RED"
    print(f"\n{problems} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
