"""Send one real goal through GeminiPlanner against the live API. Planning only.

    py scripts/live_check.py

Nothing is executed: this calls the planner and prints the Plan it returns, so
the request/response wiring can be exercised against the real model without any
tool touching the machine. The API key is read from UAIC_GEMINI_API_KEY and is
never printed. A PlannerError is printed as-is (it is already key-scrubbed).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from uaic.core import GeminiPlanner, HttpTransport, PlannerError  # noqa: E402
from uaic.core.llm import api_key  # noqa: E402

GOAL = ("In my workspace at C:/sandbox, move the file downloads/report.pdf to "
        "documents/report.pdf and move downloads/notes.txt to documents/notes.txt, "
        "then commit the git repository at C:/sandbox/proj with the message "
        "'tidy downloads'.")


def main() -> int:
    # A generous per-attempt timeout and two attempts: the live flash model is
    # occasionally slow or briefly overloaded, which 60s x 3 would compound.
    key = api_key("UAIC_GEMINI_API_KEY")
    transport = HttpTransport(secrets=(key,), max_attempts=2, backoff=3.0)
    planner = GeminiPlanner(transport=transport, timeout=120)
    print(f"model: {planner._model}")
    print(f"goal: {GOAL}\n")
    try:
        plan = planner.propose(GOAL, "live-1")
    except PlannerError as exc:
        print(f"PlannerError: {exc}")
        return 1

    print(plan.preview())
    print("\n--- raw steps ---")
    for s in plan.steps:
        print(f"  {s.seq}. {s.tool}  args={s.args}")
    print(f"\nrequested_paths: {plan.requested_paths}")
    print(f"summary: {plan.summary!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
