"""The host helper's real implementations, on temporary directories and scripted processes."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.server.host_helper_support import record

from server.host_api import (
    Checkout,
    DetachedLaunchErrorCode,
    DetachedLaunchFailure,
    HelperErrorCode,
    LiveInstanceRecord,
)
from server.host_examples import ProjectLookupError
from server.host_helper_os import (
    FileProjects,
    GitCheckoutProbe,
    OsDirectories,
    ProjectStopIntents,
    SubprocessRunLauncher,
)
from server.host_helper_requests import PathHints, ScannedEntry
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import FakeGitRepositories, run_execution_record
from vs_sim.api import ProbeResult, ProcessSpec
from vs_sim.api.testing import FakeProcessLauncher, ProcessScript, ScriptedProbe, SimThreads


def test_a_scan_marks_directories_and_links_to_them_and_stops_at_its_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "file").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "dir")
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    directories = OsDirectories()

    scan = directories.scan(str(tmp_path), 10)
    limited = directories.scan(str(tmp_path), 2)

    assert sorted(scan.entries, key=lambda entry: entry.name) == [
        ScannedEntry("dangling", is_directory=False),
        ScannedEntry("dir", is_directory=True),
        ScannedEntry("file", is_directory=False),
        ScannedEntry("link", is_directory=True),
    ]
    assert scan.complete
    assert (len(limited.entries), limited.complete) == (2, False)


def test_a_scan_raises_what_the_completion_names(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    directories = OsDirectories()
    with pytest.raises(FileNotFoundError):
        directories.scan(str(tmp_path / "missing"), 10)
    with pytest.raises(NotADirectoryError):
        directories.scan(str(tmp_path / "file"), 10)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists every directory")
def test_a_directory_this_user_cannot_list_is_permission_denied(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            OsDirectories().scan(str(locked), 10)
    finally:
        locked.chmod(0o700)


def test_hints_name_checkouts_projects_and_repositories(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "vibesys"\n')
    (checkout / ".git").write_text("gitdir: elsewhere\n")
    project = tmp_path / "project"
    (project / ".vibesys" / "tasks").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text("not toml [")
    directories = OsDirectories()

    assert directories.hints(str(checkout)) == PathHints(
        vibesys_checkout=True, vibesys_project=False, git_repository=True
    )
    assert directories.hints(str(project)) == PathHints(
        vibesys_checkout=False, vibesys_project=True, git_repository=False
    )
    assert directories.hints(str(other)) == PathHints(
        vibesys_checkout=False, vibesys_project=False, git_repository=False
    )


def test_expansion_touches_only_a_leading_tilde() -> None:
    directories = OsDirectories()
    assert directories.expand("~") == directories.home()
    assert directories.expand("/a/~b") == "/a/~b"
    assert directories.expand("rel/x") == "rel/x"


def test_projects_report_missing_and_uninitialized_directories(tmp_path: Path) -> None:
    projects = FileProjects(FakeGitRepositories().repository, SimThreads())
    with pytest.raises(ProjectLookupError) as missing:
        projects.tasks(tmp_path / "missing")
    with pytest.raises(ProjectLookupError) as bare:
        projects.require_initialized(tmp_path)

    assert missing.value.code is HelperErrorCode.PROJECT_MISSING
    assert bare.value.code is HelperErrorCode.PROJECT_UNINITIALIZED
    assert projects.named_runs(tmp_path, ()) == (tmp_path.resolve(), ())


class _NoWait:
    async def sleep(self, seconds: float) -> None:
        del seconds


def _launch(
    script: ProcessScript,
) -> tuple[LiveInstanceRecord | DetachedLaunchFailure, ProcessSpec]:
    processes = FakeProcessLauncher(clock=_NoWait(), script=lambda _spec: script)
    launcher = SubprocessRunLauncher(processes, lambda arguments: ["vibesys", *arguments])
    outcome = launcher.start(Path("/project"), ("--detach", "--task", "t"))
    (spec,) = processes.started
    return outcome, spec


def test_a_launch_runs_the_vibesys_command_in_the_project_and_reads_its_record() -> None:
    started = record("0123456789ab")
    outcome, spec = _launch(
        ProcessScript(stdout=b"log line\n" + started.model_dump_json().encode())
    )

    assert outcome == started
    assert (spec.argv, spec.cwd) == (("vibesys", "--detach", "--task", "t"), Path("/project"))


def test_a_launch_passes_on_a_typed_failure_and_types_one_that_printed_nothing() -> None:
    failure = DetachedLaunchFailure(
        code="resume_not_found", stage="resume", message="m", exit_code=2
    )
    reported, _ = _launch(ProcessScript(returncode=2, stdout=failure.model_dump_json().encode()))
    silent, _ = _launch(ProcessScript(returncode=3, stderr=b"Traceback: boom"))

    assert reported == failure
    assert isinstance(silent, DetachedLaunchFailure)
    assert silent.code == DetachedLaunchErrorCode.SERVER_START_FAILED
    assert silent.exit_code == 3
    assert "boom" in silent.message


def test_a_checkout_is_described_by_git_or_left_unknown() -> None:
    def git(argv: tuple[str, ...]) -> ProbeResult | None:
        if "rev-parse" in argv:
            return ProbeResult(0, "abc123\n")
        return None

    probe = ScriptedProbe(git)
    assert GitCheckoutProbe(Path("/src/vibesys"), probe).describe() == Checkout(
        root="/src/vibesys", commit="abc123", dirty=None
    )
    assert GitCheckoutProbe(None, probe).describe() is None
    clean = ScriptedProbe(lambda _argv: ProbeResult(0, ""))
    assert GitCheckoutProbe(Path("/src"), clean).describe() == Checkout(
        root="/src", commit=None, dirty=False
    )


def _project_with_run(root: Path) -> str:
    project = Project.open(root)
    project.state.create_project("stop", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "stop",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return manifest.run_id


def test_a_force_stop_is_recorded_in_the_runs_project(tmp_path: Path) -> None:
    run_id = _project_with_run(tmp_path)

    ProjectStopIntents().record(record("0123456789ab", run_id=run_id, project_root=str(tmp_path)))

    assert Project.open(tmp_path).state.stop_requested(run_id)


def test_a_stop_for_a_project_that_is_gone_is_left_unrecorded(tmp_path: Path) -> None:
    ProjectStopIntents().record(record("0123456789ab", project_root=str(tmp_path / "gone")))

    assert not (tmp_path / "gone").exists()
