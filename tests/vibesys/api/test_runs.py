"""Product launch retains one canonical identity across handles and events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.core_plugins import capability_plugin, failing_plugin
from tests.support.docker_environment import fake_docker_environment
from tests.support.real_git import REAL_PROCESSES
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from entrypoints.run import supervise
from launch import LaunchSettings, default_runs
from vibesys.api import (
    ComputeBackend,
    Config,
    CoreEventType,
    OrchestrationDescriptor,
    OrchestrationRegistry,
    ProfilerKind,
    ResumeRef,
    RunRequest,
    RunResult,
)
from vibesys.api import RunStatus as ProductRunStatus
from vibesys.api.request import load_task
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project, ProjectStateError, validate_run_id
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def request_template(tmp_path: Path) -> RunRequest:
    root = tmp_path / "project"
    root.mkdir()
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="identity-test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(root),
        exp_name="Display NAME / safe!",
        run_environment=fake_docker_environment(),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _fake_agent(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


@pytest.mark.asyncio
async def test_launch_allocates_identity_before_execution(request_template: RunRequest) -> None:
    registry = OrchestrationRegistry()
    registry.register_plugin(failing_plugin("identity-test"))
    runs = default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=_fake_agent,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
        )
    )
    handle = runs.start(request_template)
    allocated = handle.run_id
    assert allocated != request_template.exp_name
    assert validate_run_id(allocated) == allocated
    assert runs.attach(allocated) is handle
    assert runs.list_active() == (handle,)
    result = await handle.result()
    assert result.run_id == allocated
    events = [event async for event in handle.events()]
    assert events
    assert {event.run_id for event in events} == {allocated}
    assert runs.attach(allocated) is handle
    assert runs.list_active() == ()
    manifest = Project.open(request_template.project_root).state.load_run(allocated)
    assert manifest.display_name == request_template.exp_name

    resume_request = RunRequest.model_validate(
        {
            **request_template.model_dump(),
            "resume": {"run_id": allocated},
            "run_id": allocated,
        }
    )
    resumed = runs.resume(resume_request)
    assert resumed is not handle
    assert resumed.run_id == allocated
    assert runs.attach(allocated) is resumed
    assert runs.list_active() == (resumed,)
    resumed_result = await resumed.result()
    assert resumed_result.run_id == allocated
    resumed_events = [event async for event in resumed.events()]
    assert resumed_events
    assert {event.run_id for event in resumed_events} == {allocated}
    assert runs.attach(allocated) is resumed
    assert runs.list_active() == ()
    assert await handle.result() == result


@settings(
    max_examples=examples(pr=8, nightly=100),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(identity=st.text())
def test_preallocated_identity_uses_project_validation(
    request_template: RunRequest, identity: str
) -> None:
    data = request_template.model_dump()
    data["run_id"] = identity
    try:
        canonical = validate_run_id(identity)
    except ProjectStateError:
        with pytest.raises(ValidationError, match=r"RunRequest.run_id"):
            RunRequest.model_validate(data)
        with pytest.raises(ValidationError, match=r"ResumeRef.run_id"):
            ResumeRef(run_id=identity)
        data.update(run_id=None, resume={"run_id": identity})
        with pytest.raises(ValidationError, match=r"resume.run_id"):
            RunRequest.model_validate(data)
    else:
        assert RunRequest.model_validate(data).resolved_run_id == canonical
        assert ResumeRef(run_id=identity).run_id == canonical
        data.update(run_id=None, resume={"run_id": identity})
        assert RunRequest.model_validate(data).resolved_run_id == canonical


@pytest.mark.parametrize("identity", [None, "prior-run"])
def test_resume_accepts_only_matching_preallocated_identity(
    request_template: RunRequest, identity: str | None
) -> None:
    data = request_template.model_dump()
    data.update(run_id=identity, resume=ResumeRef(run_id="prior-run"))
    assert RunRequest.model_validate(data).resolved_run_id == "prior-run"
    data["run_id"] = "different-run"
    with pytest.raises(ValidationError, match=r"run_id must match resume.run_id"):
        RunRequest.model_validate(data)


@pytest.mark.asyncio
async def test_requested_stop_returns_typed_status_after_cleanup(
    request_template: RunRequest,
) -> None:
    registry = OrchestrationRegistry()
    # A search that proposes nothing: only the operator's stop can end this run.
    registry.register_plugin(
        capability_plugin("identity-test", strategy=SkeletonStrategy.cancelled())
    )
    runs = default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=_fake_agent,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
        )
    )
    handle = runs.start(request_template)
    handle.stop()
    result = await supervise(handle, signals=None)
    assert result.status is ProductRunStatus.STOPPED
    assert not result.succeeded
    events = [event async for event in handle.events()]
    assert any(event.type is CoreEventType.STOPPED for event in events)
    assert all(event.type is not CoreEventType.RUN_FAILED for event in events)


@pytest.mark.parametrize(
    ("succeeded", "status"),
    [(True, ProductRunStatus.COMPLETED), (False, ProductRunStatus.FAILED)],
)
def test_run_result_preserves_existing_terminal_status_derivation(
    status: ProductRunStatus, *, succeeded: bool
) -> None:
    result = RunResult(run_id="run", loop="dynamic", succeeded=succeeded)
    assert result.status is status


@pytest.mark.parametrize(
    "fields",
    [
        {"succeeded": True, "status": ProductRunStatus.FAILED},
        {"succeeded": False, "status": ProductRunStatus.COMPLETED},
        {"succeeded": True, "status": ProductRunStatus.STOPPED},
        {"succeeded": False, "status": ProductRunStatus.ACTIVE},
        {"succeeded": False, "status": ProductRunStatus.UNKNOWN},
        {"succeeded": False, "unknown": "unexpected"},
    ],
)
def test_run_result_rejects_contradictory_and_nonterminal_status(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunResult.model_validate({"run_id": "run", "loop": "dynamic", **fields})


def test_run_result_accepts_typed_stopped_outcome() -> None:
    result = RunResult(
        run_id="run", loop="dynamic", succeeded=False, status=ProductRunStatus.STOPPED
    )
    assert result.status is ProductRunStatus.STOPPED
    assert not result.succeeded
