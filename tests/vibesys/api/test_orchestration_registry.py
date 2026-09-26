"""Selection, validation, and projection through one orchestration interface."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationRegistry,
    ProfilerKind,
    RunRequest,
    RunStatus,
    RunView,
    create_session,
    open_run_store,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.context import RunSetup, RunStartHints
from vibesys.events import CoreEvent, CoreEventType, RunStartedData
from vibesys.loops.evolve.entrypoint import EvolveOrchestrator
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.multi.orchestration import (
    MultiAgentOrchestrator,
    ProfileGuidedMultiAgentOrchestrator,
)
from vibesys.loops.registry import built_in_orchestrations
from vibesys.loops.single.orchestration import (
    ProfileGuidedSingleAgentOrchestrator,
    SingleAgentOrchestrator,
)
from vibesys.orchestration import OrchestrationResumeDecision
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import AgentRole, OrchestrationPlugin, RunHost
from vs_runtime.api import RunStatus as PluginRunStatus

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


class _UnsupportedTeamDescriptorError(ValueError):
    def __init__(self) -> None:
        super().__init__("unsupported team-search descriptor")


def _discard_event(event: CoreEvent) -> None:
    del event


class _StubOrchestrator:
    calls: ClassVar[list[RunRequest]] = []
    result: ClassVar[bool] = True

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        if descriptor.id != "team-search" or descriptor.config_version != 2:
            raise _UnsupportedTeamDescriptorError
        self.setup = RunSetup()

    async def run(self, ctx: RunContext) -> bool:
        self.calls.append(ctx.request)
        return self.result


class _PluginOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(gt=0)


class _PluginState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_rounds: int


async def _run_plugin(host: RunHost, options: BaseModel) -> PluginRunStatus:
    parsed = _PluginOptions.model_validate(options)
    await host.state.commit(_PluginState(completed_rounds=parsed.max_rounds))
    return PluginRunStatus.SUCCEEDED


_SETUP_PLUGIN = OrchestrationPlugin(
    id="setup-plugin",
    agents=(AgentRole(id="worker", system_prompt="Complete the assigned work."),),
    options=_PluginOptions,
    orchestrate=_run_plugin,
    state=_PluginState,
)


def _accept_resume(
    _recorded: OrchestrationDescriptor, _requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return OrchestrationResumeDecision(descriptor=None)


def _plugin_setup(options: BaseModel) -> RunSetup:
    parsed = _PluginOptions.model_validate(options)
    return RunSetup(
        state_namespace="setup-state",
        state_slots={"state.json": _PluginState},
        resume_policy=_accept_resume,
        start_hints=RunStartHints(max_rounds=parsed.max_rounds),
        memory_paths=(".vibesys-memory/progress.md",),
    )


def _custom_request(tmp_path: Path) -> RunRequest:
    root = tmp_path / "custom-project"
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(root),
        exp_name="custom-run",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_registry_rejects_duplicate_and_missing_ids() -> None:
    registry = OrchestrationRegistry()
    registry.register("team-search", _StubOrchestrator)

    assert registry.resolve("team-search").orchestrator is _StubOrchestrator
    with pytest.raises(ValueError, match="already registered"):
        registry.register("team-search", _StubOrchestrator)
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve("missing")


@pytest.mark.parametrize("invalid_id", ["", " Team", "team/search", "TEAM", "a" * 129])
def test_registry_rejects_ids_outside_descriptor_envelope(invalid_id: str) -> None:
    registry = OrchestrationRegistry()
    with pytest.raises(ValueError, match="invalid orchestration ID"):
        registry.register(invalid_id, _StubOrchestrator)

    registry.register("team.v2", _StubOrchestrator)
    assert registry.resolve("team.v2").orchestrator is _StubOrchestrator


def test_builtin_ids_resolve_to_distinct_concrete_orchestrators() -> None:
    registry = built_in_orchestrations()
    expected = {
        "multi-agent": MultiAgentOrchestrator,
        "single-agent": SingleAgentOrchestrator,
        "profile-guided-multi-agent": ProfileGuidedMultiAgentOrchestrator,
        "profile-guided-single-agent": ProfileGuidedSingleAgentOrchestrator,
        "plain": IssueQueueOrchestrator,
        "evolve": EvolveOrchestrator,
    }
    for kind, implementation in expected.items():
        registration = registry.resolve(kind)
        assert registration.orchestrator is implementation
        assert registration.projector is not None


def test_session_executes_registered_policy_with_canonical_request(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    _StubOrchestrator.calls.clear()
    registry = OrchestrationRegistry()
    registry.register("team-search", _StubOrchestrator)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    result = asyncio.run(session.await_result())
    view = session.view()

    assert _StubOrchestrator.calls == [request]
    assert result.run_id.endswith("-custom-run")
    assert result.loop == "team-search"
    assert result.succeeded
    assert view.status is RunStatus.COMPLETED
    assert view.projection is None
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.outer_loop == "team-search"
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 4


class _Evidence(BaseModel):
    revision: int


class _EvidencePolicy(_StubOrchestrator):
    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        super().__init__(descriptor)
        self.setup = RunSetup(state_namespace="evidence", state_slots={"state.json": _Evidence})

    async def run(self, ctx: RunContext) -> bool:
        await ctx.state.checkpoint(sequence=1, writes={"state.json": _Evidence(revision=4)})
        return await super().run(ctx)


class _EvidenceProjector:
    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        state = (
            project.state.portable_namespace(run_id, "evidence")
            .slot("state.json", _Evidence)
            .load_optional()
        )
        return RunView(
            run_id=run_id,
            loop=loop,
            status=status,
            projection={"revision": state.revision} if state is not None else None,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "evidence" or not isinstance(state, _Evidence):
            return None
        return RunView(
            run_id=run_id,
            loop="team-search",
            status=RunStatus.ACTIVE,
            projection={"revision": state.revision},
        )


def test_projector_uses_same_committed_state_for_live_and_stored_views(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register(
        "team-search",
        _EvidencePolicy,
        projector=_EvidenceProjector(),
        portable_namespaces=("evidence",),
    )
    session = create_session(request, sink=_discard_event, registry=registry)
    committed: list[RunView] = []
    session.on_committed_view(lambda view, _keys: committed.append(view))

    result = asyncio.run(session.await_result())
    stored = open_run_store(Project.open(request.project_root), registry=registry).get_run(
        result.run_id
    )

    assert len(committed) == 1
    assert committed[0].projection == {"revision": 4}
    assert session.view().projection == stored.projection == {"revision": 4}
    assert stored.status is RunStatus.UNKNOWN


def test_request_rejects_parallel_legacy_selector_fields(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    assert "loop" not in RunRequest.model_fields
    assert "inner_loop" not in RunRequest.model_fields
    assert "max_rounds" not in RunRequest.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunRequest.model_validate({**request.model_dump(), "max_rounds": 2})


def test_constructor_rejects_invalid_descriptor_before_run_resources(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(id="team-search", config_version=3, options={})
        }
    )
    registry = OrchestrationRegistry()
    registry.register("team-search", _StubOrchestrator)
    with pytest.raises(ValueError, match="unsupported team-search descriptor"):
        create_session(request, sink=_discard_event, registry=registry)
    assert not (request.project_root / ".vibesys").exists()


def test_public_session_applies_registered_plugin_setup(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id=_SETUP_PLUGIN.id,
                config_version=1,
                options={"max_rounds": 3},
            )
        }
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(
        _SETUP_PLUGIN,
        setup=_plugin_setup,
        portable_namespaces=("setup-state",),
    )
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)
    session.start()

    result = asyncio.run(session.await_result())

    assert result.succeeded
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.max_rounds == 3
    assert started.data.expected_roles == ("worker",)
    stored = (
        Project.open(request.project_root)
        .state.portable_namespace(result.run_id, "setup-state")
        .slot("state.json", _PluginState)
        .load_optional()
    )
    assert stored == _PluginState(completed_rounds=3)
    prepared = registry.resolve(_SETUP_PLUGIN.id).prepare_plugin(request.orchestration)
    assert prepared.setup is not None
    assert prepared.setup.resume_policy is _accept_resume
    assert prepared.setup.start_hints is not None
    assert prepared.setup.start_hints.expected_roles == ("worker",)
    assert prepared.setup.memory_paths == (".vibesys-memory/progress.md",)


def test_registered_plugin_rejects_invalid_options_before_run_resources(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id=_SETUP_PLUGIN.id,
                config_version=1,
                options={"max_rounds": 3, "unknown": True},
            )
        }
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(_SETUP_PLUGIN, setup=_plugin_setup)

    with pytest.raises(ValidationError, match="unknown"):
        create_session(request, sink=_discard_event, registry=registry)

    assert not (request.project_root / ".vibesys").exists()


def test_registered_plugin_without_setup_derives_roles_from_declaration(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id=_SETUP_PLUGIN.id,
                config_version=1,
                options={"max_rounds": 1},
            )
        }
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(_SETUP_PLUGIN)

    prepared = registry.resolve(_SETUP_PLUGIN.id).prepare_plugin(request.orchestration)

    assert prepared.setup.start_hints is not None
    assert prepared.setup.start_hints.max_rounds is None
    assert prepared.setup.start_hints.expected_roles == ("worker",)
