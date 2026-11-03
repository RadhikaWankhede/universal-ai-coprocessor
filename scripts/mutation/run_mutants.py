"""Check that every injection test can fail: weaken its defence, expect red.

    py scripts/mutation/run_mutants.py            # the whole matrix
    py scripts/mutation/run_mutants.py --show     # also print why each went red

Each test is run in a fresh pytest process with mutants from mutants.py
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
B = "tests/test_confirmation_binding.py"
T = "tests/test_taint.py"
P = "tests/test_progress.py"
F = "tests/test_fs_batch.py"
U = "tests/test_undo_brokered.py"

# (file, substring of the test id) -> the mutants to run. Each must turn the
# test RED, unless written "NAME=GREEN": that records an attack with two
# independent defences, where removing one alone must leave the test green
# because the other still stops it. "A+B" applies both mutants at once.
# First match wins, so the more specific entries come first.
MATRIX: list[tuple[str, str, tuple[str, ...]]] = [
    (SUITE, "residual_poisoned_memory", ()),     # no code defence; see README
    (SUITE, "state_mediated_injection__reversible_swap_before_an_approved_commit[off]", ()),
    (SUITE, "state_mediated_injection__reversible_swap_before_an_approved_commit[on]",
     ("TAINT_OFF", "STAMP_OFF")),
    (SUITE, "frozen_reversible_tool__fs_delete__escalated_by_taint[careful]",
     ("TAINT_OFF", "STAMP_OFF")),
    (SUITE, "frozen_reversible_tool__fs_delete__escalated_by_taint[rubber-stamp]",
     ("TAINT_OFF", "UNDO_OFF")),
    (SUITE, "plan_confirmed_compensable_tool__git_commit__defeated_by_binding",
     ("TAINT_OFF", "BINDING_OFF=GREEN", "BINDING_OFF+TAINT_OFF")),
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
    (B, "retry_of_the_same_failed_call", ("ASK_EVERY_STEP",)),
    (B, "replan_keeps_is_covered", ("PER_CALL",)),
    (B, "second_time_asks_again", ("BINDING_OFF",)),
    (B, "user_never_saw", ("BINDING_OFF=GREEN", "BINDING_OFF+TAINT_OFF")),
    (T, "replan_after_a_read_is_tainted", ("STAMP_OFF",)),
    (T, "failure_note_alone_taints", ("STAMP_OFF", "RESULTS_ONLY_TAINT")),
    (T, "cannot_set_its_own_taint", ("STAMP_OFF",)),
    (T, "retry_keeps_the_taint", ("STAMP_OFF",)),
    (T, "next_runs_first_plan_carries", ("STAMP_OFF",)),
    (T, "outside_the_approved_plan_is_escalated", ("TAINT_OFF",)),
    (T, "without_a_ledger_nothing_counts", ("TAINT_OFF",)),
    (P, "failing_progress_sink_changes_nothing", ("PROGRESS_RAISES",)),
    (P, "sink_cannot_rewrite_the_call", ("PROGRESS_LIVE_ARGS",)),
    (P, "cancel_before_the_plan_is_shown", ("CANCEL_LOOP_OFF",)),
    (P, "cancel_mid_dispatch_finishes_that_step", ("CANCEL_LOOP_OFF",)),
    (P, "cancel_does_not_answer_a_confirmation", ("CANCEL_DISPATCH_OFF",)),
    (P, "cancelled_step_is_journalled", ("CANCEL_DISPATCH_OFF",)),
    (F, "tampered_record_cannot_send", ("RECORD_UNCHECKED",)),
    (F, "tampered_record_cannot_pull", ("RECORD_UNCHECKED",)),
    (F, "only_restores_into_the_folder", ("RECORD_UNCHECKED",)),
    (F, "destination_must_be_inside", ("SCOPE_OFF",)),
    (F, "source_folder_must_be_inside", ("SCOPE_OFF",)),
    (F, "cannot_climb_above_the_grant", ("SCOPE_OFF",)),
    (U, "outside_the_grant_is_refused", ("UNDO_UNBROKERED",)),
    (U, "is_journalled", ("UNDO_UNBROKERED",)),
    (U, "declined_inverse_does_not_run", ("UNDO_UNBROKERED",)),
    (U, "wrong_type_is_refused", ("ARG_TYPES_OFF",)),
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
    nodeids = collect(SUITE) + [n for path in ("tests/test_loop.py", B, T, P, F, U)
                                for n in collect(path) if mutants_for(n) is not None]
    problems = 0
    for nodeid in nodeids:
        name = nodeid.split("::", 1)[1]
        mutants = mutants_for(nodeid)
        if mutants is None:
            print(f"{'UNMAPPED':24} {'?':6} {name}")
            problems += 1
            continue
        if not mutants:
            print(f"{'(no mutant)':24} {'n/a':6} {name}")
            continue
        for entry in mutants:
            mutant, _, expect = entry.partition("=")
            verdict, why = run(nodeid, mutant)
            print(f"{entry:24} {verdict:6} {name}")
            if show and why:
                print(f"{'':31}{why[:150]}")
            problems += verdict != (expect or "RED")
    print(f"\n{problems} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
