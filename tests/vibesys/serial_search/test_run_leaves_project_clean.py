"""A serial run's designer leaves the project a clean baseline for the next run (#2957).

The single and multi designers kept their roadmap as files in the project root, which the run
never committed, so the next run refused to start on a dirty project. Their planning state is
now VibeSys-owned run state behind the roadmap operations (`test_roadmap_owner` shows it kept
across runs). This test plays what a designer does to the project in an earlier run, as the
product declares it: it writes into every directory its turn may write. The next run then starts
in a copy of the project (a new process, which knows nothing of what the earlier one wrote) and
must find a clean baseline.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.real_git import real_git_tracker

from launch import built_in_orchestrations
from vibesys.serial_search.api import ORCHESTRATOR
from vs_core.api import Access
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_project.api.testing import run_execution_record

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

    from vibesys.plugin_registration import OrchestrationRegistration

# Each case runs real Git processes, as the next-run check does.

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_EARLIER_RUN = "earlier-run"
_NEXT_RUN = "next-run"
_OPTIONS: dict[str, JsonValue] = {
    "interface": "service",
    "max_rounds": 2,
    "max_retries_per_round": 1,
    "official_eval_every": 1,
}


def _git(root: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _user_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "main.py")
    _git(root, "commit", "-q", "-m", "baseline")


def _start_run(root: Path, descriptor: OrchestrationDescriptor) -> Project:
    """Start the earlier run as the product does: its project, its run record, its branch."""
    tracker = real_git_tracker(root, run_id=_EARLIER_RUN, events=NullGitTrackerEvents())
    tracker.init(existing=False)
    project = Project.open(root)
    project.state.create_project("Clean project test", now=_NOW)
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "Clean project test",
            run_id=_EARLIER_RUN,
            branch=tracker.project_branch,
            vibesys_version="test",
            trusted_input_baseline=tracker.trusted_input_baseline,
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=descriptor,
            now=_NOW,
        )
    )
    tracker.snapshot_with_framework_metadata(
        f"vibesys: initialize run {_EARLIER_RUN}",
        project.state.initialization_snapshot(_EARLIER_RUN),
    )
    return project


def _designer_writes(registration: OrchestrationRegistration) -> tuple[str, ...]:
    """The directories a designer turn may write in the project, as the policy declares them."""
    if ORCHESTRATOR.access is not Access.WRITE_ARTIFACTS:
        return ()
    return registration.plugin.core.artifact_directories


@pytest.mark.parametrize("plugin_id", ["single-agent", "multi-agent"])
def test_a_serial_designer_leaves_the_project_clean_for_the_next_run(
    tmp_path: Path, plugin_id: str
) -> None:
    registration = built_in_orchestrations().resolve(plugin_id)
    extra: dict[str, JsonValue] = {"judge_every": 1} if plugin_id == "multi-agent" else {}
    options = {**_OPTIONS, **extra}
    descriptor = OrchestrationDescriptor(id=plugin_id, config_version=1, options=options)
    earlier = tmp_path / "earlier"
    _user_repository(earlier)
    _start_run(earlier, descriptor)

    for directory in _designer_writes(registration):
        written = earlier / directory / "index.md"
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text("- major-1: todo\n", encoding="utf-8")

    following = shutil.copytree(earlier, tmp_path / "next", symlinks=True)
    tracker = real_git_tracker(following, run_id=_NEXT_RUN, events=NullGitTrackerEvents())
    tracker.init(existing=False)

    assert tracker.baseline_changes() == []
