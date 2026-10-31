"""What confirmation binding costs: questions asked per completed task.

    py scripts/confirmation_cost.py

Runs the existing end-to-end tests (everything except the binding's own
tests) three times, each in a fresh pytest process:

    before    the old rule - one plan-level "yes" covers every later
              plan-level call in the run (mutant BINDING_OFF)
    after     confirmation binding as shipped - a plan-level "yes" covers
              the calls of the approved plan
    per-call  the stricter alternative - a "yes" covers only the call shown
              in that dialog (mutant PER_CALL)

Every AgentLoop.run and Executor.run_plan is recorded with its outcome and
the number of questions it actually asked. Only the counts matter here, so a
test that fails under a mode still contributes its tasks. Plain text out.

Also loaded by pytest as a plugin (`-p confirmation_cost`), which is how the
recording is installed in the child processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODES = {"before": "BINDING_OFF", "after": "", "per-call": "PER_CALL"}
EXCLUDE = "tests/test_confirmation_binding.py"


# ------------------------------------------------------------- as a plugin

def pytest_configure(config) -> None:
    out = os.environ.get("COST_OUT")
    if not out:
        return
    from uaic.runtime import AgentLoop, Executor, Outcome

    def record(kind: str, completed: bool, asked: int) -> None:
        test = os.environ.get("PYTEST_CURRENT_TEST", "?").split(" ")[0]
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"test": test, "kind": kind, "completed": completed,
                                "asked": asked}) + "\n")

    real_run = AgentLoop.run

    def run(self, request, plan_id):
        report = real_run(self, request, plan_id)
        record("loop", report.outcome is Outcome.COMPLETED, report.confirmations)
        return report
    AgentLoop.run = run

    real_plan = Executor.run_plan

    def run_plan(self, calls, caps):
        report = real_plan(self, calls, caps)
        record("plan", not report.refused and not report.stopped_early
               and len(report.executed) == len(report.steps), report.confirmations)
        return report
    Executor.run_plan = run_plan


# ------------------------------------------------------------- as a script

def measure(mutant: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cost.jsonl"
        env = dict(os.environ, COST_OUT=str(out), MUTANT=mutant,
                   PYTHONPATH=os.pathsep.join([str(ROOT / "scripts"),
                                               str(ROOT / "scripts" / "mutation")]))
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "-p", "mutants", "-p", "confirmation_cost", "tests",
                        f"--ignore={EXCLUDE}"],
                       cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


def main() -> int:
    results = {mode: measure(mutant) for mode, mutant in MODES.items()}

    print(f"{'mode':9} | {'tasks':>5} | {'completed':>9} | {'questions':>9} | "
          f"{'on completed':>12} | per completed task")
    print("-" * 78)
    for mode, rows in results.items():
        done = [r for r in rows if r["completed"]]
        asked_done = sum(r["asked"] for r in done)
        print(f"{mode:9} | {len(rows):5} | {len(done):9} | {sum(r['asked'] for r in rows):9} | "
              f"{asked_done:12} | {asked_done / len(done):.3f}" if done else "")

    # Where the modes disagree, task by task (tests are deterministic, so the
    # n-th task of a test is the same task in every mode).
    def keyed(rows):
        seen: dict[str, int] = defaultdict(int)
        out = {}
        for r in rows:
            seen[r["test"]] += 1
            out[(r["test"], seen[r["test"]])] = r
        return out
    by_mode = {mode: keyed(rows) for mode, rows in results.items()}
    keys = sorted(set().union(*by_mode.values()))
    changed = [k for k in keys
               if len({(by_mode[m].get(k) or {}).get("asked") for m in MODES}) > 1]
    print(f"\n{len(changed)} task(s) where the number of questions differs:")
    for test, n in changed:
        cells = []
        for mode in MODES:
            r = by_mode[mode].get((test, n))
            cells.append(f"{mode}={r['asked']}{'' if r['completed'] else '(not completed)'}"
                         if r else f"{mode}=-")
        print(f"  {test.split('::')[-1]} #{n}: " + ", ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
