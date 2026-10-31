"""Run the injection suite and print its evidence table.

    py scripts/injection_report.py

Every row comes from a test that ran just now: the outcome and reason code are
what that test observed through the real loop, broker and journal, recorded
with `record_property`. A row whose test failed is printed as TEST FAILED,
because what it recorded before failing is not evidence of anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "tests" / "test_injection.py"
COLUMNS = ("attack", "channel", "outcome", "mechanism", "code")
HEADINGS = ("attack", "channel", "outcome", "defeating mechanism", "reason code")


class Collector:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, str]]] = []

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call" or (report.when == "setup" and not report.passed):
            self.rows.append((report.nodeid, report.outcome, dict(report.user_properties)))


def table(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    line = lambda r: " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True))  # noqa: E731
    return "\n".join([line(rows[0]), "-+-".join("-" * w for w in widths),
                      *(line(r) for r in rows[1:])])


def main() -> int:
    sys.path.insert(0, str(ROOT))
    collector = Collector()
    status = pytest.main(["-q", "-p", "no:cacheprovider", str(SUITE)], plugins=[collector])

    rows = [HEADINGS]
    for nodeid, outcome, props in collector.rows:
        if outcome == "passed":
            rows.append(tuple(props.get(c, "?") for c in COLUMNS))
        else:
            name = nodeid.split("::")[-1]
            rows.append((name, "", f"TEST {outcome.upper()}", "", ""))
    print()
    print(table(rows))

    passed = sum(1 for _, o, _ in collector.rows if o == "passed")
    through = sum(1 for _, o, p in collector.rows
                  if o == "passed" and p.get("outcome", "").startswith("GOT THROUGH"))
    print(f"\n{passed}/{len(collector.rows)} tests passed; "
          f"{through} row(s) record an attack that got through.")
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
