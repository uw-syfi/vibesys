"""Selection, validation, and projection through one orchestration interface."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

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
from vibesys.events import CoreEvent, CoreEventType, ExperimentsChangedData, RunStartedData
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.orchestration.evolve import PLUGIN as EVOLVE_PLUGIN
from vibesys.orchestration.evolve.models import EvolveOptions
from vibesys.orchestration.issue_queue import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestration.issue_queue import IssueQueueOptions
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestration.multi import (
    PLUGIN as MULTI_PLUGIN,
)
from vibesys.orchestration.multi import (
    PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN,
)
from vibesys.orchestration.multi.models import MultiOptions
from vibesys.orchestration.single import (
    PLUGIN as SINGLE_PLUGIN,
)
from vibesys.orchestration.single import (
    PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN,
)
from vibesys.plugin_catalog import built_in_orchestrations
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import (
    AgentRole,
    OrchestrationPlugin,
    OrchestrationResumeDecision,
    PluginProjection,
    ProjectedRound,
    RunHost,
)
from vs_runtime.api import RunStatus as PluginRunStatus

if TYPE_CHECKING:
    from pathlib import Path


def _discard_event(event: CoreEvent) -> None:
    del event


class _TeamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workers: int = Field(gt=0)


_team_calls: list[str] = []


async def _run_team(host: RunHost, options: BaseModel) -> PluginRunStatus:
    _TeamOptions.model_validate(options)
    _team_calls.append(host.run_id)
    return PluginRunStatus.SUCCEEDED


_TEAM_PLUGIN = OrchestrationPlugin(
    id="team-search", agents=(), options=_TeamOptions, orchestrate=_run_team, config_version=2
)


class _PluginOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(gt=0)


class _PluginState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_rounds: int


async def _run_plugin(host: RunHost, options: BaseModel) -> PluginRunStatus:
    parsed = _PluginOptions.model_validate(options)
    for completed_rounds in range(1, parsed.max_rounds + 1):
        await host.state.commit(_PluginState(completed_rounds=completed_rounds))
    return PluginRunStatus.SUCCEEDED


def _project_plugin(state: BaseModel) -> PluginProjection:
    parsed = _PluginState.model_validate(state)
    return PluginProjection(
        payload={"completed_rounds": parsed.completed_rounds},
        rounds=tuple(
            ProjectedRound(number=number, status="completed", attempts=1, judge_verdict="pass")
            for number in range(1, parsed.completed_rounds + 1)
        ),
        experiment_revision=parsed.completed_rounds,
    )


def _accept_resume(
    _recorded: OrchestrationDescriptor, _requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return OrchestrationResumeDecision(descriptor=None)


def _project_max_rounds(options: BaseModel) -> int:
    return _PluginOptions.model_validate(options).max_rounds


_SETUP_PLUGIN = OrchestrationPlugin(
    id="setup-plugin",
    agents=(AgentRole(id="worker", system_prompt="Complete the assigned work."),),
    options=_PluginOptions,
    orchestrate=_run_plugin,
    state=_PluginState,
    project=_project_plugin,
    resume_policy=_accept_resume,
    memory_paths=(".vibesys-memory/progress.md",),
    project_max_rounds=_project_max_rounds,
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
    registry.register_plugin(_TEAM_PLUGIN)

    assert registry.resolve("team-search").plugin is _TEAM_PLUGIN
    with pytest.raises(ValueError, match="already registered"):
        registry.register_plugin(_TEAM_PLUGIN)
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve("missing")


@pytest.mark.parametrize("invalid_id", ["", " Team", "team/search", "TEAM", "a" * 129])
def test_registry_rejects_ids_outside_descriptor_envelope(invalid_id: str) -> None:
    registry = OrchestrationRegistry()
    with pytest.raises(ValueError, match="invalid orchestration plugin ID"):
        registry.register_plugin(
            OrchestrationPlugin(
                id=invalid_id, agents=(), options=_TeamOptions, orchestrate=_run_team
            )
        )

    versioned = OrchestrationPlugin(
        id="team.v2", agents=(), options=_TeamOptions, orchestrate=_run_team
    )
    registry.register_plugin(versioned)
    assert registry.resolve("team.v2").plugin is versioned


def test_builtin_catalog_selects_only_plugins() -> None:
    registry = built_in_orchestrations()
    expected = {
        "multi-agent": MULTI_PLUGIN,
        "single-agent": SINGLE_PLUGIN,
        "profile-guided-multi-agent": PROFILE_MULTI_PLUGIN,
        "profile-guided-single-agent": PROFILE_SINGLE_PLUGIN,
        "plain": ISSUE_QUEUE_PLUGIN,
        "evolve": EVOLVE_PLUGIN,
    }
    for kind, plugin in expected.items():
        registration = registry.resolve(kind)
        assert registration.plugin is plugin
        assert registration.projector is not None
        assert registration.portable_namespaces == (kind,)


def test_builtin_single_agent_executes_with_the_public_stub_backend(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="single-agent",
                config_version=1,
                options={
                    "interface": "inprocess",
                    "max_rounds": 1,
                    "max_retries_per_round": 1,
                    "judge_every": 1,
                    "official_eval_every": 1,
                    "memory_layout": "files",
                },
            )
        }
    )
    session = create_session(request, sink=_discard_event)

    result = asyncio.run(session.await_result())

    assert result.loop == "single-agent"


def test_builtin_plugin_metadata_declares_product_policy() -> None:
    registry = built_in_orchestrations()
    agent_options: dict[str, JsonValue] = {
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
        options = registration.parse_options(descriptor)
        assert isinstance(options, AgentOrchestrationOptions)
        assert registration.plugin.id == descriptor.id
        assert descriptor.options == agent_options
        assert options.max_rounds == descriptor.options["max_rounds"]
        assert registration.plugin.project_max_rounds is not None
        assert registration.plugin.project_max_rounds(options) == 4
        assert registration.plugin.memory_paths == declared_memory_paths()
        assert registration.plugin.resume_policy is not None

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
    registration = registry.resolve("plain")
    plain_options = registration.parse_options(plain)
    assert isinstance(plain_options, IssueQueueOptions)
    assert plain.options["load_levels"] == [{"rate": 4, "duration": 20, "max_tokens": 64}]
    assert plain_options.max_rounds == plain.options["max_rounds"]
    assert registration.plugin.project_max_rounds is not None
    assert registration.plugin.project_max_rounds(plain_options) == 5
    assert registration.plugin.memory_paths == ()
    assert registration.plugin.resume_policy is not None


@pytest.mark.parametrize(
    ("plugin_id", "options", "expected_tuple"),
    [
        (
            "multi-agent",
            {
                "interface": "service",
                "max_rounds": 2,
                "max_retries_per_round": 2,
                "judge_every": 1,
                "official_eval_every": 3,
                "memory_layout": "directories",
                "operator_constraints": ["Preserve ordering"],
                "metric_space": {
                    "objectives": [{"name": "throughput", "direction": "max"}],
                    "relative_noise": 0.01,
                },
            },
            ("Preserve ordering",),
        ),
        (
            "plain",
            {
                "max_rounds": 2,
                "max_attempts_per_issue": 2,
                "max_issues_per_perf_eval": 3,
                "load_levels": [{"rate": 4, "duration": 20, "max_tokens": 64}],
            },
            (4,),
        ),
        (
            "evolve",
            {
                "max_generations": 2,
                "children_per_generation": 2,
                "k_top_inspirations": 1,
                "k_random_inspirations": 1,
                "selection_temperature": 1.0,
                "frontier_bias": 0.7,
                "bootstrap_max_attempts": 3,
                "keep_deployments": False,
                "max_parallelism": 2,
                "metric_space": {
                    "objectives": [{"name": "throughput", "direction": "max"}],
                    "relative_noise": 0.01,
                },
            },
            ("throughput",),
        ),
    ],
)
def test_builtin_plugin_parsing_preserves_strict_tuples_from_persisted_json(
    plugin_id: str,
    options: dict[str, JsonValue],
    expected_tuple: tuple[str | int, ...],
) -> None:
    descriptor = OrchestrationDescriptor(
        id=plugin_id,
        config_version=1,
        options=options,
    )

    registration = built_in_orchestrations().resolve(descriptor.id)
    parsed = registration.parse_options(descriptor)

    if plugin_id == "multi-agent":
        assert isinstance(parsed, MultiOptions)
        observed = parsed.operator_constraints
    elif plugin_id == "plain":
        assert isinstance(parsed, IssueQueueOptions)
        assert parsed.load_levels is not None
        observed = tuple(level.rate for level in parsed.load_levels)
    else:
        assert isinstance(parsed, EvolveOptions)
        observed = tuple(item.name for item in parsed.metric_space.objectives)
    assert observed == expected_tuple
    compare = registration.plugin.resume_policy
    assert compare is not None
    assert compare(descriptor, descriptor).descriptor is None


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
    compare = registry.resolve(recorded.id).plugin.resume_policy
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


def test_evolve_plugin_resume_allows_only_increased_generation_budget() -> None:
    registry = built_in_orchestrations()
    options: dict[str, JsonValue] = {
        "max_generations": 2,
        "children_per_generation": 2,
        "k_top_inspirations": 1,
        "k_random_inspirations": 1,
        "selection_temperature": 1.0,
        "frontier_bias": 0.7,
        "bootstrap_max_attempts": 3,
        "keep_deployments": False,
        "max_parallelism": 2,
    }
    recorded = OrchestrationDescriptor(
        id="evolve",
        config_version=1,
        options=options,
    )
    plugin = registry.resolve("evolve").plugin
    compare = plugin.resume_policy
    assert compare is not None
    assert plugin.project_max_rounds is not None
    assert plugin.project_max_rounds(plugin.options.model_validate(recorded.options)) == 2

    assert compare(recorded, recorded).descriptor is None
    increased = recorded.model_copy(update={"options": {**recorded.options, "max_generations": 3}})
    decision = compare(recorded, increased)
    assert decision.descriptor == increased
    assert decision.requires_clean_workspace

    changed_policy = recorded.model_copy(
        update={"options": {**recorded.options, "children_per_generation": 3}}
    )
    with pytest.raises(ConfigurationError, match="children_per_generation"):
        compare(recorded, changed_policy)
    decreased = recorded.model_copy(update={"options": {**recorded.options, "max_generations": 1}})
    with pytest.raises(ConfigurationError, match=r"max_generations.*cannot decrease"):
        compare(recorded, decreased)


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
    compare = registry.resolve(recorded.id).plugin.resume_policy
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
    _team_calls.clear()
    registry = OrchestrationRegistry()
    registry.register_plugin(_TEAM_PLUGIN)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    result = asyncio.run(session.await_result())
    view = session.view()

    assert _team_calls == [result.run_id]
    assert result.run_id.endswith("-custom-run")
    assert result.loop == "team-search"
    assert result.succeeded
    assert view.status is RunStatus.COMPLETED
    assert view.projection is None
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.outer_loop == "team-search"
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 5


class _Evidence(BaseModel):
    revision: int


async def _run_evidence(host: RunHost, options: BaseModel) -> PluginRunStatus:
    _TeamOptions.model_validate(options)
    await host.state.commit(_Evidence(revision=4))
    return PluginRunStatus.SUCCEEDED


def _project_evidence(state: BaseModel) -> PluginProjection:
    evidence = _Evidence.model_validate(state)
    return PluginProjection(payload={"revision": evidence.revision})


_EVIDENCE_PLUGIN = OrchestrationPlugin(
    id="team-search",
    agents=(),
    options=_TeamOptions,
    orchestrate=_run_evidence,
    state=_Evidence,
    project=_project_evidence,
    config_version=2,
)


def test_projector_uses_same_committed_state_for_live_and_stored_views(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register_plugin(_EVIDENCE_PLUGIN)
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
    registry.register_plugin(_TEAM_PLUGIN)
    with pytest.raises(ValueError, match="requires config version 2"):
        create_session(request, sink=_discard_event, registry=registry)
    assert not (request.project_root / ".vibesys").exists()


def test_public_session_applies_registered_plugin_metadata(tmp_path: Path) -> None:
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
    registry.register_plugin(_SETUP_PLUGIN)
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
    assert [event.round_label for event in finished_rounds] == ["round-1", "round-2", "round-3"]
    experiment_changes = [
        event.data
        for event in events
        if event.type is CoreEventType.EXPERIMENTS_CHANGED
        and isinstance(event.data, ExperimentsChangedData)
        and event.data.reason != "project_attached"
    ]
    assert experiment_changes == [
        ExperimentsChangedData(reason="round_persisted", revision=2),
        ExperimentsChangedData(reason="round_persisted", revision=3),
    ]
    stored = (
        Project.open(request.project_root)
        .state.portable_namespace(result.run_id, _SETUP_PLUGIN.id)
        .slot("state.json", _PluginState)
        .load_optional()
    )
    assert stored == _PluginState(completed_rounds=3)
    assert [view.experiment_revision for view in committed] == [1, 2, 3]
    assert committed[-1] == RunView(
        run_id=result.run_id,
        loop=_SETUP_PLUGIN.id,
        status=RunStatus.ACTIVE,
        projection={"completed_rounds": 3},
        rounds=tuple(
            {
                "number": number,
                "status": "completed",
                "attempts": 1,
                "judge_verdict": "pass",
            }
            for number in range(1, 4)
        ),
        experiment_revision=3,
    )
    historical = open_run_store(Project.open(request.project_root), registry=registry).get_run(
        result.run_id
    )
    assert historical.model_copy(update={"status": RunStatus.ACTIVE}) == committed[-1]
    registration = registry.resolve(_SETUP_PLUGIN.id)
    parsed = registration.parse_options(request.orchestration)
    assert registration.plugin.resume_policy is _accept_resume
    assert registration.plugin.project_max_rounds is not None
    assert registration.plugin.project_max_rounds(parsed) == 3
    assert tuple(role.id for role in registration.plugin.agents) == ("worker",)
    assert registration.plugin.memory_paths == (".vibesys-memory/progress.md",)


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
    registry.register_plugin(_SETUP_PLUGIN)

    with pytest.raises(ValidationError, match="unknown"):
        create_session(request, sink=_discard_event, registry=registry)

    assert not (request.project_root / ".vibesys").exists()


def test_registered_plugin_derives_roles_from_declaration() -> None:
    registry = OrchestrationRegistry()
    registry.register_plugin(_SETUP_PLUGIN)

    registration = registry.resolve(_SETUP_PLUGIN.id)

    assert tuple(role.id for role in registration.plugin.agents) == ("worker",)


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
