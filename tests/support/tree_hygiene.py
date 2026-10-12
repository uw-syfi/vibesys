"""Fail a session that leaves new entries in the repository root.

A test that writes through a path relative to the working directory drops its
output in the repository root when it runs from there. Nothing in that test's
own assertions notices, so the stray files only show up as untracked changes
after the run. Comparing the root's top-level names before and after the session
turns the leak into a failure that names the entry.

Hidden entries and build output are tooling's own and are ignored.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_TOOLING = frozenset({"__pycache__", "build", "dist", "node_modules"})
_STASH = pytest.StashKey[frozenset[str]]()


def top_level_entries(root: Path) -> frozenset[str]:
    """The names directly under ``root`` that a test could have created by accident."""
    return frozenset(
        entry.name
        for entry in os.scandir(root)
        if not entry.name.startswith(".")
        and entry.name not in _TOOLING
        and not entry.name.endswith(".egg-info")
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Record the root's entries; xdist workers leave the check to the controller."""
    if not hasattr(session.config, "workerinput"):
        session.config.stash[_STASH] = top_level_entries(session.config.rootpath)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when it added entries to the repository root."""
    before = session.config.stash.get(_STASH, None)
    if before is None:
        return
    leaked = sorted(top_level_entries(session.config.rootpath) - before)
    if not leaked:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"ERROR: tests left new entries in the repository root: {leaked}", red=True
        )
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
