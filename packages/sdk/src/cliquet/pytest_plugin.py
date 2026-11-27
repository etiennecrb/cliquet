"""Pytest plugin used by `cliquet test`: fail the run when an invariant never refused.

Publishing requires that every invariant refuses at least one change in the tests,
so a rule that can't fail is caught before it goes live.
"""

from typing import Any

from cliquet import testing


def never_refused() -> dict[str, list[str]]:
    """Per app name, the invariants that no test saw refuse a change."""
    missing: dict[str, list[str]] = {}
    for key, (app, invariants) in testing.seen.items():
        left = [name for name in invariants if name not in testing.refused.get(key, set())]
        if left:
            missing[app] = left
    return missing


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    missing = never_refused()
    if missing and exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    missing = never_refused()
    if not missing:
        if testing.seen:
            terminalreporter.write_line("cliquet: every invariant refused at least once")
        return
    terminalreporter.write_sep("=", "cliquet: invariants that never refused", red=True)
    for app, names in missing.items():
        for name in names:
            terminalreporter.write_line(
                f"{app}: {name} never refused a change. Add a test where it does."
            )
