"""Agent confinement hides the run's own record on every mechanism that enforces hidden paths.

Regression for #2993: only `.vibesys/state/local` was hidden, so a confined agent could read
`.vibesys/state/runs/<run>/core-store` (every role's turns, the judge's reviews, the trusted
measurements) and could write the file a resume loads. The project's path policy is built over a
real project on disk, and each mechanism lowers it as it would for an agent: Docker's empty
masks (read through a Fake daemon's mounts), bubblewrap's tmpfs overlays, Seatbelt's denies.
Landlock cannot enforce hidden paths, so it names this one with the others it drops.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.run.project_policy import build_project_path_policy
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_sandbox.api import DockerSandbox, HostSandbox, LandlockSandbox, SeatbeltSandbox
from vs_sandbox.api.docker_workspace import docker_project_path_resources
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads, examples

if TYPE_CHECKING:
    from vs_sandbox.api import ProjectPathPolicy

_RUN_IDS = st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True)
_STORE = b'{"turns": "every role", "reviews": "the judge", "measurements": "trusted"}\n'


def _project_with_run_record(root: Path, run_id: str) -> tuple[Project, Path]:
    """A project whose run `run_id` has a core store, and the store's file."""
    root.mkdir(parents=True)
    project = Project.open(root)
    project.state.create_project("test", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "Run",
        run_id=run_id,
        trusted_input_baseline="a" * 40,
        branch=f"vibesys/{run_id}",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    project.state.state_store_namespace(run_id).write_bytes("store.json", _STORE)
    project.state.candidate_worktrees_directory(run_id).mkdir(parents=True)
    return project, root / ".vibesys/state/runs" / run_id / "core-store" / "store.json"


def _policy(root: Path) -> ProjectPathPolicy:
    return build_project_path_policy(root, evaluator_source=None)


def _index(argv: list[str], *parts: str) -> int:
    for index in range(len(argv) - len(parts) + 1):
        if argv[index : index + len(parts)] == list(parts):
            return index
    pytest.fail(f"sequence not found: {parts!r}")


@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(run_id=_RUN_IDS)
def test_the_policy_hides_every_runs_record_and_the_machine_local_state(
    tmp_path_factory: pytest.TempPathFactory, run_id: str
) -> None:
    root = tmp_path_factory.mktemp("project") / "project"
    _, store = _project_with_run_record(root, run_id)

    policy = _policy(root)

    relative = PurePosixPath(store.relative_to(root).as_posix())
    assert {Path(".vibesys/state/runs"), Path(".vibesys/state/local")} <= set(policy.hidden_paths)
    assert any(relative.is_relative_to(hidden.as_posix()) for hidden in policy.hidden_paths)
    assert Path(".vibesys") in policy.read_only_paths


def test_a_docker_agent_sees_an_empty_directory_where_the_run_record_is(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _, store = _project_with_run_record(root, "run-1")
    engine = FakeDockerEngine(tmp_path / "engine", launcher=FakePipedProcessLauncher())
    resources = docker_project_path_resources(
        _policy(root), root, mask_root=tmp_path / "operator" / "hidden"
    )

    with DockerSandbox(
        host_workspace=str(root),
        image="agent-image",
        resources=resources,
        docker=engine,
        threads=SimThreads(),
    ):
        (container,) = engine.containers()

        in_container = f"/workspace/{store.relative_to(root).as_posix()}"
        # Resolve the agent's path the way the container does: the deepest mount wins.
        mount = max((m for m in container.mounts if in_container.startswith(m)), key=len)
        seen = container.mounts[mount] / in_container[len(mount) :].lstrip("/")

        assert mount == "/workspace/.vibesys/state/runs"
        assert mount in container.read_only_mounts
        assert not seen.exists()
        assert not any(container.mounts[mount].iterdir())
        assert store.read_bytes() == _STORE
        # The machine-local state is masked the same way.
        assert "/workspace/.vibesys/state/local" in container.mounts


def test_a_bubblewrap_agent_gets_a_tmpfs_over_the_run_record_after_its_binds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _project_with_run_record(root, "run-1")
    sandbox = HostSandbox(
        workspace=root,
        bwrap_path="/usr/bin/bwrap",
        system_read_roots=(),
        project_path_policy=_policy(root),
    )

    argv = sandbox.wrap(["agent"])

    workspace_bind = _index(argv, "--bind", str(root), str(root))
    configuration = _index(argv, "--ro-bind", str(root / ".vibesys"), str(root / ".vibesys"))
    runs = _index(argv, "--tmpfs", str(root / ".vibesys/state/runs"))
    local = _index(argv, "--tmpfs", str(root / ".vibesys/state/local"))
    assert workspace_bind < configuration < min(runs, local) < _index(argv, "--chdir", str(root))


def test_a_seatbelt_agent_is_denied_reads_and_writes_of_the_run_record(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _project_with_run_record(root, "run-1")
    sandbox = SeatbeltSandbox(
        workspace=root,
        sandbox_exec_path="/usr/bin/sandbox-exec",
        project_path_policy=_policy(root),
    )

    profile = sandbox.profile()

    allow = f'(allow file-read* file-write* (subpath "{root.resolve()}"))'
    for hidden in ("runs", "local"):
        deny = f'(deny file-read* file-write* (subpath "{(root / ".vibesys/state" / hidden).resolve()}"))'
        assert profile.index(allow) < profile.index(deny)


def test_landlock_names_the_run_record_among_the_hidden_paths_it_cannot_enforce(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _project_with_run_record(root, "run-1")
    # ``/tmp`` is dropped from the scratch roots: the test session's runtime root lives
    # under it, and Landlock refuses a grant that reaches the operator's runtime root.
    sandbox = LandlockSandbox(
        workspace=root, project_path_policy=_policy(root), scratch_write_roots=("/dev", "/proc")
    )

    gaps = "\n".join(sandbox.unenforced_policy())

    assert ".vibesys/state/runs" in gaps
    assert ".vibesys/state/local" in gaps
