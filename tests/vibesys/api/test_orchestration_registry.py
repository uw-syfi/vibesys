"""Selection and isolation contracts for the orchestration boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel, ValidationError

import vibesys.api as public_api
from vibesys.api import (
    ComputeBackend,
    Config,
    Orchestration,
    OrchestrationRegistry,
    OrchestrationRunRequest,
    ProfilerKind,
    VibeSysRuntime,
    built_in_orchestrations,
    create_session,
    open_run_store,
)
from vibesys.api._dispatch import dispatch_loop
from vibesys.api._orchestrations.agent import AgentOrchestration
from vibesys.api._orchestrations.contracts import RunDescription
from vibesys.api._orchestrations.evolve import EvolveOrchestration
from vibesys.api._orchestrations.plain import PlainOrchestration
from vibesys.api._orchestrations.profile_guided import ProfileGuidedOrchestration
from vibesys.api.contracts import LoopKind, RunRequest, RunResult, RunStatus, RunView
from vibesys.api.entry import default_request
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.events import CoreEvent, CoreEventType, RunStartedData
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, OrchestrationRunManifest, Project

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.session import _LocalRunSession
    from vibesys.orchestration import ResumeProjection

_EXAMPLE = "examples/model-serving/whisper-large-v3"


class _StubOrchestration:
    def __init__(self, *, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[RunRequest, VibeSysRuntime]] = []

    def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
        self.calls.append((request, runtime))
        return self.result


def test_registry_rejects_duplicate_and_missing_ids() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=True)
    registry.register(LoopKind.PLAIN, implementation)

    assert registry.resolve(LoopKind.PLAIN) is not implementation
    with pytest.raises(ValueError, match="already registered"):
        registry.register(LoopKind.PLAIN, _StubOrchestration(result=False))
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve(LoopKind.EVOLVE)


@pytest.mark.parametrize("invalid_id", ["", " Team", "team/search", "TEAM", "a" * 129])
def test_registry_rejects_ids_outside_descriptor_envelope(invalid_id: str) -> None:
    registry = OrchestrationRegistry()
    with pytest.raises(ValueError, match="invalid orchestration ID"):
        registry.register(invalid_id, _StubOrchestration(result=True))

    registry.register("team.v2", _StubOrchestration(result=True))
    assert registry.resolve("team.v2") is not None


def test_dispatch_uses_injected_registry_without_built_in_loop_calls() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=False)
    registry.register(LoopKind.EVOLVE, implementation)
    request = RunRequest.model_construct(loop=LoopKind.EVOLVE)
    integration = LocalRunIntegration()

    assert dispatch_loop(request, integration, registry) is False
    assert implementation.calls[0][0] is request
    assert implementation.calls[0][1] is not integration


def test_builtin_ids_resolve_to_their_own_implementations() -> None:
    registry = built_in_orchestrations()

    expected = {
        LoopKind.AGENT: AgentOrchestration,
        LoopKind.PROFILE_GUIDED: ProfileGuidedOrchestration,
        LoopKind.PLAIN: PlainOrchestration,
        LoopKind.EVOLVE: EvolveOrchestration,
    }
    for kind, implementation_type in expected.items():
        implementation = registry.resolve(kind)
        assert type(implementation) is implementation_type
        assert isinstance(implementation, Orchestration)


@pytest.mark.parametrize(
    "case",
    [
        (LoopKind.AGENT, "multi-agent", "vibesys.loops.agent.loop", "agent"),
        (LoopKind.AGENT, "single-agent", "vibesys.loops.agent.loop", "agent"),
        (LoopKind.PROFILE_GUIDED, "multi-agent", "vibesys.loops.agent.loop", "profile-guided"),
        (LoopKind.PROFILE_GUIDED, "single-agent", "vibesys.loops.agent.loop", "profile-guided"),
        (LoopKind.PLAIN, "multi-agent", "vibesys.loops.plain.loop", None),
        (LoopKind.EVOLVE, "multi-agent", "vibesys.loops.evolve.loop", None),
    ],
)
def test_builtin_execute_dispatches_directly_through_registered_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[LoopKind, str, str, str | None],
) -> None:
    kind, inner_loop, loop_module, expected_outer = case
    request = _custom_request(tmp_path).model_copy(
        update={
            "loop": kind,
            "orchestration": None,
            "inner_loop": inner_loop,
            "objective": "Improve the queue.",
        }
    )
    integration = LocalRunIntegration()
    runtime = cast("VibeSysRuntime", SimpleNamespace(legacy_integration=integration))
    calls: list[dict[str, object]] = []

    def fake_loop(**kwargs: object) -> bool:
        calls.append(kwargs)
        return True

    entrypoint = {
        LoopKind.AGENT: "run_agent_loop",
        LoopKind.PROFILE_GUIDED: "run_agent_loop",
        LoopKind.PLAIN: "run_plain_loop",
        LoopKind.EVOLVE: "run_evolve_loop",
    }[kind]
    monkeypatch.setattr(f"{loop_module}.{entrypoint}", fake_loop)

    implementation = built_in_orchestrations().resolve(kind)
    assert implementation.execute(request, runtime)
    assert len(calls) == 1
    assert calls[0]["integration"] is integration
    if expected_outer is not None:
        assert calls[0]["outer_loop"] == expected_outer
        assert calls[0]["inner_loop"] == inner_loop


def _custom_request(tmp_path: Path) -> RunRequest:
    project_root = tmp_path / "custom-project"
    project_root.mkdir()
    (project_root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (project_root / "queue.py").write_text("VALUE = 1\n")
    (project_root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        exp_name="custom-run",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_session_executes_unknown_registered_id_and_reports_it(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    implementation = _StubOrchestration(result=True)
    registry = OrchestrationRegistry()
    registry.register("team-search", implementation)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    result = asyncio.run(session.await_result())
    view = session.view()

    assert implementation.calls[0][0] is request
    assert implementation.calls[0][0].orchestration == request.orchestration
    assert result.run_id.endswith("-custom-run")
    assert view.run_id == result.run_id
    assert result.loop == "team-search"
    assert result.succeeded is True
    assert view.loop == "team-search"
    assert view.status is RunStatus.COMPLETED
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.outer_loop == "team-search"
    assert started.data.expected_roles == ()
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 4


def test_policy_owns_start_metadata_and_live_and_stored_views(tmp_path: Path) -> None:
    class ProjectingPolicy(_StubOrchestration):
        def describe(self, request: RunRequest) -> RunDescription:
            assert request.orchestration_id == "team-search"
            return RunDescription(max_rounds=9, expected_roles=("scout", "reviewer"))

        def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
            assert project.root == request.project_root
            return RunView(
                run_id=run_id,
                loop=loop,
                status=status,
                projection={"kind": "team-search", "workers": ["scout", "reviewer"]},
            )

    request = _custom_request(tmp_path)
    policy = ProjectingPolicy(result=True)
    registry = OrchestrationRegistry()
    registry.register("team-search", policy)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    result = asyncio.run(session.await_result())

    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.max_rounds == 9
    assert started.data.expected_roles == ("scout", "reviewer")
    assert session.view().projection == {"kind": "team-search", "workers": ["scout", "reviewer"]}
    stored = open_run_store(Project.open(request.project_root), registry=registry).get_run(
        result.run_id
    )
    assert stored.projection == session.view().projection
    assert public_api.agent_projection(stored) is None
    assert RunView.model_validate_json(stored.model_dump_json()) == stored
    assert stored.status is RunStatus.UNKNOWN


def test_custom_policy_projects_committed_state_for_its_own_namespace(tmp_path: Path) -> None:
    class Evidence(BaseModel):
        revision: int

    class EvidencePolicy(_StubOrchestration):
        def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
            # Exercise commits while the run is active and its ID is provisioned.
            session._integration.publish_committed_state(  # noqa: SLF001
                "unrelated", Evidence(revision=1)
            )
            session._integration.publish_committed_state(  # noqa: SLF001
                "evidence", Evidence(revision=4), changed_keys=("candidate-4",)
            )
            return super().execute(request, runtime)

        def project_committed(
            self, namespace: str, state: BaseModel, *, run_id: str
        ) -> RunView | None:
            if namespace != "evidence":
                return None
            assert isinstance(state, Evidence)
            return RunView(
                run_id=run_id,
                loop="team-search",
                status=RunStatus.ACTIVE,
                projection={"kind": "evidence", "revision": state.revision},
            )

    request = _custom_request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register("team-search", EvidencePolicy(result=True))

    def discard(event: CoreEvent) -> None:
        del event

    session = cast("_LocalRunSession", create_session(request, sink=discard, registry=registry))
    committed: list[tuple[RunView, tuple[str, ...] | None]] = []
    session.on_committed_view(lambda view, keys: committed.append((view, keys)))

    result = asyncio.run(session.await_result())

    assert len(committed) == 1
    view, keys = committed[0]
    assert view.run_id == result.run_id
    assert view.loop == "team-search"
    assert view.projection == {"kind": "evidence", "revision": 4}
    assert keys == ("candidate-4",)


def test_descriptor_request_runs_without_legacy_loop_fields(tmp_path: Path) -> None:
    class CompletePolicy(_StubOrchestration):
        def prepare(self, request: OrchestrationRunRequest, runtime: VibeSysRuntime) -> None:
            del request, runtime

        def describe(self, request: OrchestrationRunRequest) -> RunDescription:
            del request
            return RunDescription()

        def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
            del project
            return RunView(run_id=run_id, loop=loop, status=status)

        def project_committed(
            self, namespace: str, state: BaseModel, *, run_id: str
        ) -> RunView | None:
            del namespace, state, run_id
            return None

        def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
            del manifest
            raise NotImplementedError

    legacy = _custom_request(tmp_path)
    assert legacy.orchestration is not None
    request = OrchestrationRunRequest(
        project_root=legacy.project_root,
        orchestration=legacy.orchestration,
        config=legacy.config,
        input_bundle=legacy.input_bundle,
        exp_name="descriptor-run",
        run_environment=legacy.run_environment,
        agent_backend=legacy.agent_backend,
        profiler_kind=legacy.profiler_kind,
        backend=legacy.backend,
    )
    assert "loop" not in OrchestrationRunRequest.model_fields
    assert "inner_loop" not in OrchestrationRunRequest.model_fields
    assert "max_rounds" not in OrchestrationRunRequest.model_fields
    assert "search_policy" not in OrchestrationRunRequest.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OrchestrationRunRequest.model_validate({**request.model_dump(), "max_rounds": 2})
    registry = OrchestrationRegistry()
    policy = CompletePolicy(result=True)
    registry.register("team-search", policy)
    assert isinstance(policy, Orchestration)
    assert registry.resolve("team-search") is policy

    def discard(event: CoreEvent) -> None:
        del event

    session = create_session(request, sink=discard, registry=registry)
    result = asyncio.run(session.await_result())

    assert result.succeeded
    assert result.loop == "team-search"
    assert policy.calls[0][0] is request
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 4


def test_builtin_adapter_rejects_descriptor_only_request_before_run_setup(tmp_path: Path) -> None:
    legacy = _custom_request(tmp_path)
    request = OrchestrationRunRequest(
        project_root=legacy.project_root,
        orchestration=OrchestrationDescriptor(id="agent", config_version=1, options={}),
        config=legacy.config,
        input_bundle=legacy.input_bundle,
        exp_name="builtin-collision",
    )
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record)
    session.start()
    with pytest.raises(ValueError, match="built-in orchestration 'agent' requires RunRequest"):
        asyncio.run(session.await_result())

    assert [event.type for event in events] == [CoreEventType.RUN_FAILED]
    assert not (request.project_root / ".vibesys").exists()


def test_custom_policy_id_matching_builtin_name_stays_a_string(tmp_path: Path) -> None:
    legacy = _custom_request(tmp_path)
    request = OrchestrationRunRequest(
        project_root=legacy.project_root,
        orchestration=OrchestrationDescriptor(id="agent", config_version=1, options={}),
        config=legacy.config,
        input_bundle=legacy.input_bundle,
        exp_name="custom-agent-name",
        run_environment=legacy.run_environment,
        agent_backend=legacy.agent_backend,
        profiler_kind=legacy.profiler_kind,
        backend=legacy.backend,
    )
    registry = OrchestrationRegistry()
    registry.register("agent", _StubOrchestration(result=True))

    def discard(event: CoreEvent) -> None:
        del event

    session = create_session(request, sink=discard, registry=registry)
    result = asyncio.run(session.await_result())

    assert result.succeeded
    assert result.loop == "agent"
    assert type(result.loop) is str
    assert type(session.view().loop) is str


def test_describe_failure_emits_failed_event_and_closes_session(tmp_path: Path) -> None:
    class FailingPolicy(_StubOrchestration):
        def describe(self, request: RunRequest) -> RunDescription:
            del request
            message = "invalid policy metadata"
            raise RuntimeError(message)

    request = _custom_request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register("team-search", FailingPolicy(result=True))
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)
    session.start()

    with pytest.raises(RuntimeError, match="invalid policy metadata"):
        asyncio.run(session.await_result())

    assert [event.type for event in events] == [CoreEventType.RUN_FAILED]


def test_custom_selection_validates_descriptor_and_preserves_builtin_request_enum(
    tmp_path: Path, repo_root: Path
) -> None:
    custom = _custom_request(tmp_path)
    assert custom.orchestration_id == "team-search"
    assert custom.orchestration is not None
    assert custom.orchestration.options == {"workers": 3}
    assert (
        RunRequest.model_validate_json(custom.model_dump_json()).orchestration_id == "team-search"
    )

    built_in = default_request(Project.open(repo_root / _EXAMPLE), LoopKind.AGENT)
    assert public_api.LoopKind is LoopKind
    assert public_api.RunRequest is RunRequest
    assert built_in.loop is LoopKind.AGENT
    assert built_in.selected_loop is LoopKind.AGENT
    built_in_result = RunResult(run_id="builtin", loop=LoopKind.AGENT, succeeded=True)
    assert built_in_result.loop == "agent"
    assert type(built_in_result.loop) is str
    assert RunResult.model_validate_json(built_in_result.model_dump_json()).loop == "agent"
    assert (
        RunResult(run_id="builtin", loop=built_in.orchestration_id, succeeded=True).loop == "agent"
    )

    values = built_in.model_dump()
    with pytest.raises(ValidationError, match="select exactly one"):
        RunRequest.model_validate({**values, "orchestration": custom.orchestration})
    with pytest.raises(ValidationError, match="select exactly one"):
        RunRequest.model_validate({**values, "loop": None})
    with pytest.raises(ValidationError, match="built-in orchestration IDs"):
        RunRequest.model_validate(
            {
                **values,
                "loop": None,
                "orchestration": OrchestrationDescriptor(id="agent", config_version=1, options={}),
            }
        )
