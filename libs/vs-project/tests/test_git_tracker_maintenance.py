"""Regression: a tracker's Git writes start no background maintenance.

Git runs auto maintenance after a commit, and from Git 2.47 it detaches that
process by default. A detached process outlives the command, and so the run,
and keeps writing under ``.git`` while the caller removes or reuses the
repository. This flaked ``test_any_planned_id_and_title_reach_a_trusted_adopted_round``
on CI with ``Directory not empty: .../project/.git``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import GitTracker, NullGitTrackerEvents

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_LABELS = st.lists(
    st.text(st.characters(categories=("L", "N")), min_size=1, max_size=20),
    min_size=1,
    max_size=3,
)


def _started_children(trace: Path) -> list[list[str]]:
    """Return the argv of every child process Git started, from its trace2 log."""
    if not trace.exists():
        return []
    events = (json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines())
    return [event["argv"] for event in events if event.get("event") == "child_start"]


@settings(max_examples=3, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(labels=_LABELS)
def test_tracker_commits_start_no_git_maintenance(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    labels: list[str],
) -> None:
    root = tmp_path_factory.mktemp("project")
    trace = tmp_path_factory.mktemp("trace") / "git-trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    (root / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = GitTracker(root, run_id="maintenance-run", events=NullGitTrackerEvents())

    tracker.init(existing=False)
    for value, label in enumerate(labels, start=1):
        (root / "main.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        tracker.snapshot(label)

    children = _started_children(trace)
    assert [argv for argv in children if {"maintenance", "gc"} & set(argv)] == []
