"""Selection, validation, and projection through one orchestration interface."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigurationError,
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
from vibesys.orchestration import OrchestrationResumeDecision
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestrations.issue_queue import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestrations.multi import (
    PLUGIN as MULTI_PLUGIN,
)
from vibesys.orchestrations.multi import (
    PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN,
)
from vibesys.orchestrations.single import (
    PLUGIN as SINGLE_PLUGIN,
)
from vibesys.orchestrations.single import (
    PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN,
)
from vibesys.plugin_catalog import built_in_orchestrations
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import (
    AgentRole,
    OrchestrationPlugin,
    PluginProjection,
    ProjectedRound,
    RunHost,
)
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


def _project_plugin(state: BaseModel) -> PluginProjection:
    parsed = _PluginState.model_validate(state)
    return PluginProjection(
        payload={"completed_rounds": parsed.completed_rounds},
        rounds=(
            ProjectedRound(
                number=parsed.completed_rounds,
                status="completed",
                attempts=1,
                judge_verdict="pass",
            ),
        ),
        experiment_revision=parsed.completed_rounds,
    )


_SETUP_PLUGIN = OrchestrationPlugin(
    id="setup-plugin",
    agents=(AgentRole(id="worker", system_prompt="Complete the assigned work."),),
    options=_PluginOptions,
    orchestrate=_run_plugin,
    state=_PluginState,
    project=_project_plugin,
)


def _accept_resume(
    _recorded: OrchestrationDescriptor, _requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return OrchestrationResumeDecision(descriptor=None)


def _plugin_setup(options: BaseModel) -> RunSetup:
    parsed = _PluginOptions.model_validate(options)
    return RunSetup(
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


def test_builtin_catalog_selects_plugins_and_keeps_only_evolve_legacy() -> None:
    registry = built_in_orchestrations()
    expected = {
        "multi-agent": MULTI_PLUGIN,
        "single-agent": SINGLE_PLUGIN,
        "profile-guided-multi-agent": PROFILE_MULTI_PLUGIN,
        "profile-guided-single-agent": PROFILE_SINGLE_PLUGIN,
        "plain": ISSUE_QUEUE_PLUGIN,
    }
    for kind, plugin in expected.items():
        registration = registry.resolve(kind)
        assert registration.plugin is plugin
        assert registration.orchestrator is None
        assert registration.projector is not None
        assert registration.portable_namespaces == (kind,)

    legacy = registry.resolve("evolve")
    assert legacy.orchestrator is EvolveOrchestrator
    assert legacy.plugin is None
    assert legacy.projector is not None


def test_builtin_plugin_setup_is_private_product_wiring() -> None:
    registry = built_in_orchestrations()
    agent_options = {
        "interface": "service",
        "max_rounds": 4,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 3,
        "memory_layout": "directories",
    }
    descriptors = (
        OrchestrationDescriptor(id="single-agent", config_version=1, options=agent_options),
        OrchestrationDescriptor(id="multi-agent", config_version=1, options=agent_options),
    )
    for descriptor in descriptors:
        registration = registry.resolve(descriptor.id)
        prepared = registration.prepare_plugin(descriptor)
        assert prepared.plugin.id == descriptor.id
        assert descriptor.options == agent_options
        assert prepared.options.max_rounds == descriptor.options["max_rounds"]
        assert prepared.setup.start_hints is not None
        assert prepared.setup.start_hints.max_rounds == 4
        assert prepared.setup.memory_paths == declared_memory_paths()
        assert prepared.setup.resume_policy is not None

    plain = OrchestrationDescriptor(
        id="plain",
        config_version=1,
        options={
            "max_rounds": 5,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 3,
            "load_levels": [{"rate": 4, "duration": 20, "max_tokens": 64}],
        },
    )
    prepared_plain = registry.resolve("plain").prepare_plugin(plain)
    assert plain.options["load_levels"] == [{"rate": 4, "duration": 20, "max_tokens": 64}]
    assert prepared_plain.options.max_rounds == plain.options["max_rounds"]
    assert prepared_plain.setup.start_hints is not None
    assert prepared_plain.setup.start_hints.max_rounds == 5
    assert prepared_plain.setup.memory_paths == ()
    assert prepared_plain.setup.resume_policy is not None


def test_builtin_plugins_allow_only_increased_total_budget_on_resume() -> None:
    registry = built_in_orchestrations()
    recorded = OrchestrationDescriptor(
        id="plain",
        config_version=1,
        options={
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 3,
            "load_levels": [{"rate": 4, "duration": 20, "max_tokens": 64}],
        },
    )
    prepared = registry.resolve(recorded.id).prepare_plugin(recorded)
    compare = prepared.setup.resume_policy
    assert compare is not None

    increased = recorded.model_copy(update={"options": {**recorded.options, "max_rounds": 3}})
    decision = compare(recorded, increased)
    assert decision.descriptor == increased
    assert decision.requires_clean_workspace

    changed_levels = recorded.model_copy(
        update={
            "options": {
                **recorded.options,
                "load_levels": [{"rate": 8, "duration": 20, "max_tokens": 64}],
            }
        }
    )
    with pytest.raises(ConfigurationError, match="load_levels"):
        compare(recorded, changed_levels)
    with pytest.raises(ConfigurationError, match="cannot decrease"):
        compare(
            recorded, recorded.model_copy(update={"options": {**recorded.options, "max_rounds": 1}})
        )


def test_hypothesis_plugin_resume_uses_its_exact_option_schema() -> None:
    registry = built_in_orchestrations()
    recorded = OrchestrationDescriptor(
        id="single-agent",
        config_version=1,
        options={
            "interface": "service",
            "max_rounds": 2,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 3,
            "memory_layout": "directories",
        },
    )
    compare = registry.resolve(recorded.id).prepare_plugin(recorded).setup.resume_policy
    assert compare is not None

    changed = recorded.model_copy(
        update={"options": {**recorded.options, "judge_every": 2, "max_rounds": 3}}
    )
    with pytest.raises(ConfigurationError, match="judge_every"):
        compare(recorded, changed)

    wrong_preset = recorded.model_copy(
        update={"options": {**recorded.options, "profile_guided": {"command": ["true"]}}}
    )
    with pytest.raises(ValidationError, match="profile_guided"):
        compare(recorded, wrong_preset)


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
    registry.register_plugin(_SETUP_PLUGIN, setup=_plugin_setup)
    events: list[CoreEvent] = []
    committed: list[RunView] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)
    session.on_committed_view(lambda view, _keys: committed.append(view))
    session.start()

    result = asyncio.run(session.await_result())

    assert result.succeeded
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.max_rounds == 3
    assert started.data.expected_roles == ("worker",)
    finished_rounds = [event for event in events if event.type is CoreEventType.ROUND_FINISHED]
    assert [event.round_label for event in finished_rounds] == ["round-3"]
    stored = (
        Project.open(request.project_root)
        .state.portable_namespace(result.run_id, _SETUP_PLUGIN.id)
        .slot("state.json", _PluginState)
        .load_optional()
    )
    assert stored == _PluginState(completed_rounds=3)
    assert committed == [
        RunView(
            run_id=result.run_id,
            loop=_SETUP_PLUGIN.id,
            status=RunStatus.ACTIVE,
            projection={"completed_rounds": 3},
            rounds=(
                {
                    "number": 3,
                    "status": "completed",
                    "attempts": 1,
                    "judge_verdict": "pass",
                },
            ),
            experiment_revision=3,
        )
    ]
    historical = open_run_store(Project.open(request.project_root), registry=registry).get_run(
        result.run_id
    )
    assert historical.model_copy(update={"status": RunStatus.ACTIVE}) == committed[0]
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


def test_plugin_registration_derives_projection_and_portable_state_only_when_declared() -> None:
    state_only = OrchestrationPlugin(
        id="state-only",
        agents=(),
        options=_PluginOptions,
        orchestrate=_run_plugin,
        state=_PluginState,
    )
    stateless = OrchestrationPlugin(
        id="stateless",
        agents=(),
        options=_PluginOptions,
        orchestrate=_run_plugin,
    )
    registry = OrchestrationRegistry()

    registry.register_plugin(state_only)
    registry.register_plugin(stateless)

    state_registration = registry.resolve(state_only.id)
    assert state_registration.projector is None
    assert state_registration.portable_namespaces == (state_only.id,)
    stateless_registration = registry.resolve(stateless.id)
    assert stateless_registration.projector is None
    assert stateless_registration.portable_namespaces == ()
