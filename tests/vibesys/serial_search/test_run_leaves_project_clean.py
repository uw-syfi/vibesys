"""A serial run's agents leave the project a clean baseline for the next run (#2957, #2963).

The single and multi designers kept their roadmap as files in the project root, and the multi
profiler its evidence below `profile/`. The run never committed them, so the next run refused to
start on a dirty project. The roadmap is now VibeSys-owned run state behind the roadmap
operations (`test_roadmap_owner` shows it kept across runs), and the profiler's findings travel
in its reply. This test plays what the run-scoped agents do to the project in an earlier run, as
the product declares it: it writes into every directory a turn that may write in the project is
granted. The next run then starts in a copy of the project (a new process, which knows nothing
of what the earlier one wrote) and must find a clean baseline.
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
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_project.api.testing import run_execution_record
from vs_runtime.api import WorkspaceAccess

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


def _project_writes(registration: OrchestrationRegistration) -> tuple[str, ...]:
    """The directories the run's agents may write in the project, as the plugin declares them.

    A role that is not limited to artifacts writes nowhere in the project (it reads, or works in
    its own candidate workspace), so the policy's directories are open only when one is.
    """
    plugin = registration.plugin
    if any(agent.workspace_access is WorkspaceAccess.LIMITED for agent in plugin.agents):
        return plugin.core.artifact_directories
    return ()


@pytest.mark.parametrize("plugin_id", ["single-agent", "multi-agent"])
def test_a_serial_run_leaves_the_project_clean_for_the_next_run(
    tmp_path: Path, plugin_id: str
) -> None:
    registration = built_in_orchestrations().resolve(plugin_id)
    extra: dict[str, JsonValue] = {"judge_every": 1} if plugin_id == "multi-agent" else {}
    options = {**_OPTIONS, **extra}
    descriptor = OrchestrationDescriptor(id=plugin_id, config_version=1, options=options)
    earlier = tmp_path / "earlier"
    _user_repository(earlier)
    _start_run(earlier, descriptor)

    for directory in _project_writes(registration):
        written = earlier / directory / "index.md"
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text("- major-1: todo\n", encoding="utf-8")

    following = shutil.copytree(earlier, tmp_path / "next", symlinks=True)
    tracker = real_git_tracker(following, run_id=_NEXT_RUN, events=NullGitTrackerEvents())
    tracker.init(existing=False)

    assert tracker.baseline_changes() == []
