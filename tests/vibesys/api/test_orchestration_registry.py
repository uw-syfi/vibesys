"""Selection, validation, and projection through one orchestration interface."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from tests.support.core_plugins import failing_plugin
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.real_git import REAL_PROCESSES
from tests.support.task_project import write_task

from entrypoints.production import production_timing
from launch import LaunchSettings, built_in_orchestrations, create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationRegistry,
    PluginProjection,
    ProfilerKind,
    RunRequest,
    RunStatus,
    RunView,
)
from vibesys.api.request import load_task
from vibesys.events import CoreEvent, CoreEventType, RunStartedData
from vibesys.orchestration.dynamic import PLUGIN as DYNAMIC_PLUGIN
from vibesys.orchestration.evolve import PLUGIN as EVOLVE_PLUGIN
from vibesys.orchestration.evolve.launch import EvolveLaunch
from vibesys.orchestration.input_check import PLUGIN as INPUT_CHECK_PLUGIN
from vibesys.orchestration.issue_queue import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestration.issue_queue.launch import IssueQueueLaunch
from vibesys.orchestration.multi import (
    PLUGIN as MULTI_PLUGIN,
)
from vibesys.orchestration.multi import (
    PROFILE_GUIDED_PLUGIN as PROFILE_MULTI_PLUGIN,
)
from vibesys.orchestration.multi.launch import MultiLaunch
from vibesys.orchestration.single import (
    PLUGIN as SINGLE_PLUGIN,
)
from vibesys.orchestration.single import (
    PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN,
)
from vibesys.orchestration.single.launch import SingleLaunch
from vibesys.plugin_catalog import OrchestrationRegistration
from vibesys.run.contracts import RoundSummary
from vibesys.run.core_services import CoreCompositionError
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import (
    AgentRole,
    OrchestrationResumeDecision,
)
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher

if TYPE_CHECKING:
    from pathlib import Path


def _discard_event(event: CoreEvent) -> None:
    del event


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


_SETTINGS = LaunchSettings(
    blocking=ThreadBlockingRunner(),
    network=UnixNetwork(),
    timing=production_timing(),
    processes=REAL_PROCESSES,
    piped_processes=FakePipedProcessLauncher(),
    backend_factory=host_container_backend,
    agent_client_factory=_resumable_client,
)


class _TeamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workers: int = Field(gt=0)


_TEAM_PLUGIN = failing_plugin("team-search", options=_TeamOptions, config_version=2)


class _PluginOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: int = Field(gt=0)


def _accept_resume(
    _recorded: OrchestrationDescriptor, _requested: OrchestrationDescriptor
) -> OrchestrationResumeDecision:
    return OrchestrationResumeDecision(descriptor=None)


def _project_max_rounds(options: BaseModel) -> int:
    return _PluginOptions.model_validate(options).max_rounds


_SETUP_PLUGIN = failing_plugin(
    "setup-plugin",
    agents=(AgentRole(id="worker", system_prompt="Complete the assigned work."),),
    options=_PluginOptions,
    artifact_directories=(".vibesys-memory",),
)
_SETUP_REGISTRATION = OrchestrationRegistration(
    plugin=_SETUP_PLUGIN,
    resume_policy=_accept_resume,
    project_max_rounds=_project_max_rounds,
)


def _custom_request(tmp_path: Path) -> RunRequest:
    root = tmp_path / "custom-project"
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
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(root),
        exp_name="custom-run",
        run_environment=fake_docker_environment(),
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
        registry.register_plugin(failing_plugin(invalid_id, options=_TeamOptions))

    versioned = failing_plugin("team.v2", options=_TeamOptions)
    registry.register_plugin(versioned)
    assert registry.resolve("team.v2").plugin is versioned


def test_builtin_catalog_selects_only_plugins() -> None:
    registry = built_in_orchestrations()
    expected = {
        "check-input": INPUT_CHECK_PLUGIN,
        "dynamic": DYNAMIC_PLUGIN,
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


def test_builtin_single_agent_refuses_the_stub_backend_before_any_turn(
    tmp_path: Path,
) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="single-agent",
                config_version=1,
                options={
                    "interface": "inprocess",
                    "max_rounds": 1,
                    "max_retries_per_round": 1,
                    "official_eval_every": 1,
                },
            )
        }
    )
    session = create_session(
        request,
        sink=_discard_event,
        settings=LaunchSettings(
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            backend_factory=host_container_backend,
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
        ),
    )

    # A core strategy runs durable, resumable sessions; the stub backend offers none, so the
    # run is refused at composition, naming the missing capability, before any turn.
    with pytest.raises(CoreCompositionError, match="AgentTurnExecutor"):
        asyncio.run(session.await_result())


def test_builtin_plugin_metadata_declares_product_policy() -> None:
    registry = built_in_orchestrations()
    single_options: dict[str, JsonValue] = {
        "interface": "service",
        "max_rounds": 4,
        "max_retries_per_round": 2,
        "official_eval_every": 3,
    }
    # Only the multi search has a judge to ask every so often.
    multi_options: dict[str, JsonValue] = {**single_options, "judge_every": 1}
    cases = (
        (
            OrchestrationDescriptor(id="single-agent", config_version=1, options=single_options),
            SingleLaunch,
            ("roadmap",),
        ),
        (
            OrchestrationDescriptor(id="multi-agent", config_version=1, options=multi_options),
            MultiLaunch,
            ("roadmap", "profile"),
        ),
    )
    # A core strategy declares only the artifact directories its run-scoped turns write, and
    # resumes with exact options (no round-budget variant until core can extend limits, #2013).
    for descriptor, launch, paths in cases:
        registration = registry.resolve(descriptor.id)
        options = registration.parse_options(descriptor)
        assert isinstance(options, launch)
        assert registration.plugin.id == descriptor.id
        assert options.max_rounds == descriptor.options["max_rounds"]
        assert registration.project_max_rounds is not None
        assert registration.project_max_rounds(options) == 4
        assert registration.plugin.core.artifact_directories == paths
        assert registration.resume_policy is None

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
    assert isinstance(plain_options, IssueQueueLaunch)
    assert plain.options["load_levels"] == [{"rate": 4, "duration": 20, "max_tokens": 64}]
    assert plain_options.max_rounds == plain.options["max_rounds"]
    assert registration.project_max_rounds is not None
    assert registration.project_max_rounds(plain_options) == 5
    assert registration.plugin.core.artifact_directories == ()
    # A core run's limits are fixed in its committed state, so it resumes with exact options.
    assert registration.resume_policy is None


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
        assert isinstance(parsed, MultiLaunch)
        observed = parsed.operator_constraints
    elif plugin_id == "plain":
        assert isinstance(parsed, IssueQueueLaunch)
        assert parsed.load_levels is not None
        observed = tuple(level.rate for level in parsed.load_levels)
    else:
        assert isinstance(parsed, EvolveLaunch)
        observed = tuple(item.name for item in parsed.metric_space.objectives)
    assert observed == expected_tuple
    if plugin_id == "plain":
        return
    compare = registration.resume_policy
    if plugin_id in {"multi-agent", "evolve"}:
        assert compare is None  # a core strategy resumes with exact options
        return
    assert compare is not None
    assert compare(descriptor, descriptor).descriptor is None


def test_the_issue_queue_resumes_with_exact_options_until_core_can_extend_its_limits() -> None:
    """A core run's limits are fixed when it starts, so a larger round budget cannot resume it."""
    assert built_in_orchestrations().resolve("plain").resume_policy is None


def test_evolve_plugin_resumes_with_its_exact_options() -> None:
    """Core limits are fixed in the committed state (design Q3), so no budget may change."""
    registry = built_in_orchestrations()
    options: dict[str, JsonValue] = {
        "max_generations": 2,
        "children_per_generation": 2,
        "k_top_inspirations": 1,
        "k_random_inspirations": 1,
        "selection_temperature": 1.0,
        "frontier_bias": 0.7,
        "bootstrap_max_attempts": 3,
        "max_parallelism": 2,
    }
    recorded = OrchestrationDescriptor(id="evolve", config_version=1, options=options)
    registration = registry.resolve("evolve")
    assert registration.resume_policy is None
    assert registration.project_max_rounds is not None
    assert (
        registration.project_max_rounds(
            registration.plugin.options.model_validate(recorded.options)
        )
        == 2
    )


def test_session_executes_registered_policy_with_canonical_request(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    registry = OrchestrationRegistry()
    registry.register_plugin(_TEAM_PLUGIN)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(
        request,
        sink=record,
        registry=registry,
        settings=_SETTINGS,
    )

    session.start()
    result = asyncio.run(session.await_result())
    view = session.view()

    assert result.run_id.endswith("-custom-run")
    assert result.loop == "team-search"
    assert not result.succeeded
    assert view.status is RunStatus.FAILED
    assert view.projection is None
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.outer_loop == "team-search"
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 5


def test_product_projection_contract_is_strict_and_immutable() -> None:
    projection = PluginProjection(
        payload={"summary": "complete"},
        rounds=(RoundSummary(number=1, status="completed", attempts=2),),
        experiment_revision=4,
    )

    assert projection.rounds[0].attempts == 2
    with pytest.raises(ValidationError):
        projection.__setattr__("experiment_revision", 5)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PluginProjection.model_validate({"payload": None, "unknown": True})
    with pytest.raises(ValidationError):
        RoundSummary.model_validate({"number": "1", "status": "completed", "attempts": 2})


def test_request_rejects_parallel_legacy_selector_fields(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    assert "loop" not in RunRequest.model_fields
    assert "inner_loop" not in RunRequest.model_fields
    assert "max_rounds" not in RunRequest.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunRequest.model_validate({**request.model_dump(), "max_rounds": 2})


def _authored_tree(root: Path) -> frozenset[str]:
    """Every path under the project's ``.vibesys``: only the authored task before a run."""
    return frozenset(path.relative_to(root).as_posix() for path in (root / ".vibesys").rglob("*"))


def test_constructor_rejects_invalid_descriptor_before_run_resources(tmp_path: Path) -> None:
    request = _custom_request(tmp_path).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(id="team-search", config_version=3, options={})
        }
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(_TEAM_PLUGIN)
    authored = _authored_tree(request.project_root)
    with pytest.raises(ValueError, match="requires config version 2"):
        create_session(
            request,
            sink=_discard_event,
            registry=registry,
            settings=_SETTINGS,
        )
    assert _authored_tree(request.project_root) == authored


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
    registry.register(_SETUP_REGISTRATION)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry, settings=_SETTINGS)
    session.start()

    asyncio.run(session.await_result())

    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.max_rounds == 3
    assert started.data.expected_roles == ("worker",)
    registration = registry.resolve(_SETUP_PLUGIN.id)
    parsed = registration.parse_options(request.orchestration)
    assert registration.resume_policy is _accept_resume
    assert registration.project_max_rounds is not None
    assert registration.project_max_rounds(parsed) == 3
    assert tuple(role.id for role in registration.plugin.agents) == ("worker",)
    assert registration.plugin.core.artifact_directories == (".vibesys-memory",)


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
    authored = _authored_tree(request.project_root)

    with pytest.raises(ValidationError, match="unknown"):
        create_session(
            request,
            sink=_discard_event,
            registry=registry,
            settings=_SETTINGS,
        )

    assert _authored_tree(request.project_root) == authored


def test_registered_plugin_derives_roles_from_declaration() -> None:
    registry = OrchestrationRegistry()
    registry.register_plugin(_SETUP_PLUGIN)

    registration = registry.resolve(_SETUP_PLUGIN.id)

    assert tuple(role.id for role in registration.plugin.agents) == ("worker",)


class _OwnProjector:
    """A projector that reads durable state this registration does not declare."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        del project
        return RunView(run_id=run_id, loop=loop, status=status, experiment_revision=7)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        del namespace, state, run_id
        return None


def test_plugin_registers_its_own_projector() -> None:
    plugin = failing_plugin("own-projector", options=_PluginOptions)
    projector = _OwnProjector()

    registration = OrchestrationRegistration(plugin=plugin, projector=projector)

    assert registration.projector is projector
