"""Run profiling requirements fail before public session construction has effects."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest
from tests.support.core_plugins import failing_plugin
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.real_git import REAL_PROCESSES
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from launch import LaunchSettings, create_session
from vibesys.api import (
    AgentOutputChunkData,
    ComputeBackend,
    Config,
    ConfigurationError,
    CoreEvent,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    ResumeRef,
    RunRequest,
)
from vibesys.api.profilers import ProfilerPreflightResult
from vibesys.api.request import RunEnvironmentSpec, load_task
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


_PREFLIGHT_WARNING = (
    "[profiler] Resolved profiler 'linux_cpu' is not usable on this host: "
    "perf_event_paranoid_restrictive, perf_stat_unavailable. "
    "--profiler auto is falling back to 'none'."
)


class _UnavailableNativeProfiler:
    """Deterministic Fake for the application-wired native preflight effect."""

    def __init__(self) -> None:
        self.calls: list[ProfilerKind] = []

    def __call__(self, kind: ProfilerKind) -> ProfilerPreflightResult:
        self.calls.append(kind)
        return ProfilerPreflightResult(
            kind,
            usable=False,
            diagnostics=("perf_event_paranoid_restrictive", "perf_stat_unavailable"),
        )


class _RecordingBackendFactory:
    """Fake backend factory that exposes whether resource assembly reached it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> FakeComputeBackend:
        self.calls += 1
        return FakeComputeBackend(threads=SimThreads())


def _profiler_registry() -> OrchestrationRegistry:
    registry = OrchestrationRegistry()
    registry.register_plugin(failing_plugin("profiler-preflight-test"))
    return registry


def _native_profiler_request(root: Path, requested: ProfilerKind) -> RunRequest:
    project = root / "native-project"
    project.mkdir()
    write_task(
        project,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["true"]\n'
        '[profile]\ncommand = ["true"]\n',
        objective="Improve the program.\n",
    )
    return RunRequest(
        project_root=project,
        run_id="profiler-preflight-run",
        orchestration=OrchestrationDescriptor(
            id="profiler-preflight-test", config_version=1, options={}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(project),
        exp_name="profiler-preflight",
        run_environment=fake_docker_environment(),
        profiler_kind=requested,
        backend=ComputeBackend.CPU,
    )


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _request(root: Path, *, service: bool, profile: bool, requested: ProfilerKind) -> RunRequest:
    project = root / "project"
    project.mkdir()
    manifest = (
        'version = 1\n[agent]\ndomain = "llm-serving"\n'
        '[accuracy]\ncommand = ["python", "check.py"]\n'
        '[benchmark]\ncommand = ["python", "benchmark.py"]\n'
    )
    if profile:
        manifest += '[profile]\ncommand = ["python", "diagnostic.py"]\n'
    write_task(project, manifest, objective="Improve serving latency.\n")
    config_path = root / "slurm.toml"
    operator_config = (
        '[slurm]\nname = "test-cluster"\nremote_workspace_root = "/remote/runs"\n'
        '[slurm.transport]\nkind = "ssh"\nhost = "cluster"\n'
    )
    if service:
        operator_config += (
            '[vibesys.service]\ncommand = ["python", "serve.py", "--port", '
            '"VIBESYS_DYNAMIC_PORT"]\n'
            'readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"\n'
            "startup_timeout_seconds = 700\n"
        )
    config_path.write_text(operator_config, encoding="utf-8")
    return RunRequest(
        project_root=project,
        input_bundle=load_task(project),
        orchestration=OrchestrationDescriptor(
            id="multi-agent",
            config_version=1,
            options={
                "interface": "inprocess",
                "max_rounds": 1,
                "max_retries_per_round": 1,
                "judge_every": 1,
                "official_eval_every": 1,
            },
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        exp_name="profile-preflight",
        runs_dir=root / "runs",
        profiler_kind=requested,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(config_path)}),
        backend=ComputeBackend.ROCM,
    )


@pytest.mark.parametrize("requested", [ProfilerKind.AUTO, ProfilerKind.ROCPROF, ProfilerKind.NONE])
@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
def test_profile_workload_requirement_precedes_provisioning(
    tmp_path: Path, requested: ProfilerKind, *, service: bool, profile: bool, resumed: bool
) -> None:
    """Exhaust the finite requirement matrix for new and resumed sessions."""
    request = _request(tmp_path, service=service, profile=profile, requested=requested)
    if resumed:
        request = request.model_copy(update={"resume": ResumeRef(run_id="prior-run")})
    events: list[CoreEvent] = []
    before = set(tmp_path.rglob("*"))

    def record(event: CoreEvent) -> None:
        events.append(event)

    if service and not profile and requested is not ProfilerKind.NONE:
        with pytest.raises(ConfigurationError, match=r"profile\.command") as error:
            create_session(
                request,
                sink=record,
                settings=LaunchSettings(
                    timing=production_timing(),
                    processes=REAL_PROCESSES,
                    piped_processes=FakePipedProcessLauncher(),
                    backend_factory=host_container_backend,
                    blocking=ThreadBlockingRunner(),
                    network=UnixNetwork(),
                ),
            )
        assert error.value.diagnostic.code == "profile_workload_invalid"
        assert error.value.diagnostic.stage == "profiler_validation"
    else:
        session = create_session(
            request,
            sink=record,
            settings=LaunchSettings(
                timing=production_timing(),
                processes=REAL_PROCESSES,
                piped_processes=FakePipedProcessLauncher(),
                backend_factory=host_container_backend,
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
            ),
        )
        session.close()
    assert events == []
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "requested",
    [
        kind
        for kind in ProfilerKind
        if kind not in {ProfilerKind.AUTO, ProfilerKind.NONE, ProfilerKind.ROCPROF}
    ],
)
def test_incompatible_profiler_is_a_typed_early_configuration_failure(
    tmp_path: Path, requested: ProfilerKind
) -> None:
    request = _request(tmp_path, service=True, profile=True, requested=requested)
    before = set(tmp_path.rglob("*"))

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ConfigurationError, match="--profiler") as error:
        create_session(
            request,
            sink=discard,
            settings=LaunchSettings(
                timing=production_timing(),
                processes=REAL_PROCESSES,
                piped_processes=FakePipedProcessLauncher(),
                backend_factory=host_container_backend,
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
            ),
        )
    assert error.value.diagnostic.code == "profiler_incompatible"
    assert error.value.diagnostic.stage == "profiler_validation"
    assert set(tmp_path.rglob("*")) == before


def test_auto_native_preflight_failure_records_none_without_profiler_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    request = _native_profiler_request(tmp_path, ProfilerKind.AUTO)
    preflight = _UnavailableNativeProfiler()
    backend = _RecordingBackendFactory()
    git = FakeGitRepositories()
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(
        request,
        sink=record,
        settings=LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=_profiler_registry(),
            backend_factory=backend,
            git_repository=git.repository,
            profiler_preflight=preflight,
        ),
    )
    session.start()

    result = asyncio.run(session.await_result())

    assert result.loop == "profiler-preflight-test"
    assert preflight.calls == [ProfilerKind.LINUX_CPU]
    assert backend.calls == 1
    manifest = Project.open(request.project_root).state.load_run(request.resolved_run_id)
    assert manifest.execution.requested_profiler == ProfilerKind.AUTO.value
    assert manifest.execution.resolved_profiler == ProfilerKind.NONE.value
    assert not (request.project_root / "linux_cpu_profiler").exists()
    emitted_diagnostics = [
        event.data.content.removesuffix("\n")
        for event in events
        if isinstance(event.data, AgentOutputChunkData) and event.data.channel == "diagnostic"
    ]
    assert emitted_diagnostics.count(_PREFLIGHT_WARNING) == 1
    persisted_logs = "".join(
        path.read_text(encoding="utf-8")
        for path in Project.log_directory_for(request.project_root, request.resolved_run_id).glob(
            "run-*.log"
        )
    )
    assert persisted_logs.count(_PREFLIGHT_WARNING) == 1


def test_auto_native_preflight_failure_through_default_launch_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _native_profiler_request(tmp_path, ProfilerKind.AUTO)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    git_executable = shutil.which("git")
    assert git_executable is not None
    executable_dir = tmp_path / "executables"
    executable_dir.mkdir()
    (executable_dir / "git").symlink_to(git_executable)
    monkeypatch.setenv("PATH", str(executable_dir))

    def discard(event: CoreEvent) -> None:
        del event

    session = create_session(
        request,
        sink=discard,
        settings=LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=_profiler_registry(),
            backend_factory=_RecordingBackendFactory(),
            git_repository=FakeGitRepositories().repository,
        ),
    )

    result = asyncio.run(session.await_result())

    assert result.loop == "profiler-preflight-test"
    manifest = Project.open(request.project_root).state.load_run(request.resolved_run_id)
    assert manifest.execution.resolved_profiler == ProfilerKind.NONE.value


def test_explicit_native_preflight_failure_precedes_backend_and_project_effects(
    tmp_path: Path,
) -> None:
    request = _native_profiler_request(tmp_path, ProfilerKind.LINUX_CPU)
    preflight = _UnavailableNativeProfiler()
    backend = _RecordingBackendFactory()
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(
        request,
        sink=record,
        settings=LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=_profiler_registry(),
            backend_factory=backend,
            profiler_preflight=preflight,
        ),
    )
    session.start()
    before = _tree(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        asyncio.run(session.await_result())

    assert error.value.diagnostic.code == "profiler_preflight_failed"
    assert error.value.diagnostic.stage == "profiler_preflight"
    assert preflight.calls == [ProfilerKind.LINUX_CPU]
    assert backend.calls == 0
    assert _tree(tmp_path) == before
    assert all(
        not isinstance(event.data, AgentOutputChunkData)
        or _PREFLIGHT_WARNING not in event.data.content
        for event in events
    )
