"""pytest terminal summary hook for Golden Set reports."""

from __future__ import annotations

from typing import Any


def pytest_terminal_summary(terminalreporter: Any) -> None:
    counts: dict[tuple[str, str], int] = {}
    for status_key, reports in terminalreporter.stats.items():
        for report in reports or ():
            if getattr(report, "when", None) != "call":
                continue
            nodeid = getattr(report, "nodeid", "")
            if "tests/golden/test_golden_set.py" not in nodeid:
                continue
            mode = "real" if "real" in nodeid else "stub"
            outcome = {
                "passed": "Pass",
                "failed": "Fail",
                "error": "Error",
                "skipped": "Skipped",
                "xfailed": "Xfail",
            }.get(status_key, status_key)
            counts[(mode, outcome)] = counts.get((mode, outcome), 0) + 1

    if not counts:
        return

    terminalreporter.write_sep("=", "Golden Set Summary")
    for mode in ("stub", "real"):
        terminalreporter.write_line(
            f"Mode: {mode:<5} | "
            f"Pass: {counts.get((mode, 'Pass'), 0):<3} | "
            f"Xfail: {counts.get((mode, 'Xfail'), 0):<3} | "
            f"Fail: {counts.get((mode, 'Fail'), 0):<3} | "
            f"Error: {counts.get((mode, 'Error'), 0):<3} | "
            f"Skipped: {counts.get((mode, 'Skipped'), 0):<3}"
        )
    terminalreporter.write_sep("=", "Golden Set Summary")
