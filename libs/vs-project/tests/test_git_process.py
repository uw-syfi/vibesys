"""Regression: Git commands started through ``run_git`` start no background maintenance.

Git runs auto maintenance after a commit and, from Git 2.47, detaches it. A
detached process outlives the command and keeps writing under ``.git`` while the
caller removes or moves the repository ("Directory not empty: .git").
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import run_git

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@local")


def _started_children(trace: Path) -> list[list[str]]:
    """Return the argv of every child process Git started, from its trace2 log."""
    if not trace.exists():
        return []
    events = (json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines())
    return [event["argv"] for event in events if event.get("event") == "child_start"]


@settings(max_examples=3, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(commits=st.integers(min_value=1, max_value=4))
def test_commits_through_run_git_start_no_maintenance(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    commits: int,
) -> None:
    repo = tmp_path_factory.mktemp("repo")
    trace = tmp_path_factory.mktemp("trace") / "git-trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    # Make Git eager to maintain, so only the VibeSys config can stop it.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")

    assert run_git(["init", "-q"], cwd=repo).returncode == 0
    for value in range(commits):
        (repo / "file.txt").write_text(f"{value}\n", encoding="utf-8")
        run_git(["add", "."], cwd=repo)
        committed = run_git([*_IDENTITY, "commit", "-q", "-m", f"c{value}"], cwd=repo)
        assert committed.returncode == 0

    children = _started_children(trace)
    assert [argv for argv in children if {"maintenance", "gc"} & set(argv)] == []
