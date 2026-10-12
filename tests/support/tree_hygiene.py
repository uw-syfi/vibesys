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
    from collections.abc import Generator
    from pathlib import Path

    from _pytest.terminal import TerminalReporter

_TOOLING = frozenset({"__pycache__", "build", "dist", "node_modules"})
_BEFORE = pytest.StashKey[frozenset[str]]()
_LEAKED = pytest.StashKey[list[str]]()


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
        session.config.stash[_BEFORE] = top_level_entries(session.config.rootpath)


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session: pytest.Session) -> Generator[None, object, object]:
    """Compare the root once the tests are done, before session-finish tooling writes its output.

    Tooling that records results at session finish (shard durations, reports)
    runs after this point, so it never needs an allow-list entry.
    """
    result = yield
    before = session.config.stash.get(_BEFORE, None)
    if before is not None:
        leaked = sorted(top_level_entries(session.config.rootpath) - before)
        if leaked:
            session.config.stash[_LEAKED] = leaked
            session.testsfailed += 1
    return result


def pytest_terminal_summary(terminalreporter: TerminalReporter, config: pytest.Config) -> None:
    """Name the leaked entries in the summary, which xdist and ``-q`` both show."""
    leaked = config.stash.get(_LEAKED, None)
    if leaked:
        terminalreporter.section("repository root leak", sep="=", red=True)
        terminalreporter.write_line(
            f"tests left new entries in the repository root: {leaked}", red=True
        )
