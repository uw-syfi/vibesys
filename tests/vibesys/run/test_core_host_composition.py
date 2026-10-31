"""A product host for a plugin with a ``core`` slot composes every core service."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.wrappers_pb import StringValue
from tests.support.container_credentials import set_container_cli_credentials
from tests.support.core_plugins import core_policy
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import EmptyOptions

from vibesys.api._session import run_plugin
from vibesys.api.testing import FakeProfilerPreflight
from vibesys.composition import resolve_agent_specs
from vibesys.config import AgentCfg, Config
from vibesys.constants import ComputeBackend
from vibesys.events import CoreEventType
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import RunRequest
from vibesys.run.core_services import CoreCompositionError
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import (
    AgentCapabilities,
    AgentQuotaError,
    AgentTurnRequest,
    CapacityStops,
    QuotaCondition,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_core.api import (
    Capabilities,
    validate_startup,
)
from vs_project.api import OrchestrationDescriptor, Project
from vs_runlog.api import RunLogReader, read_committed_records
from vs_runtime.api import (
    AgentRole,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
    RunStatus,
)
from vs_runtime.api.core import RunTiming
from vs_runtime.api.step_runner import fold_log
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import OsThreads, SleepingClock, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.core_services import CoreServices
    from vs_agent.api import AgentClientProtocol, CapacityGate

DIGEST_LENGTH = 64


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


class _GateRecordingClient(FakeAgentClient):
    """A resumable fake client that remembers every capacity gate it was given."""

    def __init__(self) -> None:
        super().__init__(
            capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
        )
        self.gates: list[CapacityGate] = []

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        self.gates.append(gate)
        super().set_capacity_gate(gate)


def _policy(
    templates: Path,
    strategy: SkeletonStrategy | None = None,
    deadline_seconds: float = 1000.0,
    *,
    profile_capture_withheld: bool = False,
) -> CorePolicy:
    policy = core_policy(templates, strategy, deadline_seconds)

    def plan(context: CoreRunContext) -> CorePlan:
        return replace(policy.plan(context), profile_capture_withheld=profile_capture_withheld)

    return replace(policy, plan=plan)


def _plugin(templates: Path, *, profile_capture_withheld: bool = False) -> OrchestrationPlugin:
    return OrchestrationPlugin(
        id="core-test",
        agents=(AgentRole(id="implementer", system_prompt="Implement."),),
        options=EmptyOptions,
        core=_policy(templates, profile_capture_withheld=profile_capture_withheld),
    )


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )


def _request(
    project_root: Path,
    config: Config | None = None,
    profiler_kind: ProfilerKind = ProfilerKind.NONE,
) -> RunRequest:
    return RunRequest(
        run_environment=fake_docker_environment(),
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="core-test", config_version=1, options={}),
        config=config or Config.model_validate({"model": {"name": "claude-sonnet-5"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="core-test",
        agent_backend="stub" if profiler_kind is ProfilerKind.NONE else "cli",
        cli_provider="claude",
        profiler_kind=profiler_kind,
        backend=ComputeBackend.CPU,
    )


def _open(
    tmp_path: Path,
    *,
    client_factory: Callable[..., AgentClientProtocol] | None,
    journal: bool,
    config: Config | None = None,
    emitted: tuple[str, ...] = (),
) -> Callable[[], CoreServices]:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    clock = VirtualClock(at=0.0)
    integration = LocalRunIntegration(
        clock=clock,
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> CoreServices:
        try:
            async with open_product_core_host(
                _request(project_root, config),
                integration,
                plugin=_plugin(templates),
                options=EmptyOptions(),
                timing=RunTiming(clock, OsThreads()),
                agent_client_factory=client_factory,
                backend_factory=host_container_backend,
                invocation_store_factory=(
                    (lambda _state, _key: FakeAgentInvocationStore()) if journal else None
                ),
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            ) as host:
                for text in emitted:
                    integration.events.emit(CoreEventType.FRAMEWORK_WARNING, text)
                return host.services
        finally:
            integration.close()

    return lambda: run_virtual(clock, exercise())


def _open_timed_by(tmp_path: Path, journal_clock: SleepingClock, host_clock: VirtualClock) -> None:
    """Open the host over an integration timed by ``journal_clock`` and a host timed by its own."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=journal_clock,
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> None:
        try:
            async with open_product_core_host(
                _request(project_root),
                integration,
                plugin=_plugin(templates),
                options=EmptyOptions(),
                timing=RunTiming(host_clock, OsThreads()),
                agent_client_factory=_resumable_client,
                backend_factory=host_container_backend,
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            ):
                pass
        finally:
            integration.close()

    run_virtual(host_clock, exercise())


def _run_diagnostics(tmp_path: Path, *, profile_capture_withheld: bool) -> str:
    """The text a run with a chosen profiler reports on its event stream while opening."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    clock = VirtualClock(at=0.0)
    integration = LocalRunIntegration(
        clock=clock,
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> str:
        try:
            async with open_product_core_host(
                _request(project_root, profiler_kind=ProfilerKind.LINUX_CPU),
                integration,
                plugin=_plugin(templates, profile_capture_withheld=profile_capture_withheld),
                options=EmptyOptions(),
                timing=RunTiming(clock, OsThreads()),
                agent_client_factory=_resumable_client,
                backend_factory=host_container_backend,
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                profiler_preflight=lambda kind: ProfilerPreflightResult(kind=kind, usable=True),
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
            ):
                return "\n".join(repr(event) for event in integration.events.read())
        finally:
            integration.close()

    return run_virtual(clock, exercise())


def test_core_host_yields_services_whose_state_starts_the_strategy(tmp_path: Path) -> None:
    services = _open(tmp_path, client_factory=_resumable_client, journal=True)()
    declaration = services.search.declaration
    state = services.state
    catalog = services.catalog
    offered = Capabilities(lifecycle=frozenset(), operations=catalog.offered_operations)
    # The catalog offers every operation the policy registered, and the search's declaration
    # is satisfied by them.
    offered_kinds = {op.kind for op in catalog.offered_operations}
    assert offered_kinds == {
        operation.registration.descriptor.kind for operation in _policy(tmp_path).operations
    }
    assert {op.kind for op in validate_startup(declaration, offered).operations} <= offered_kinds
    assert state.run.facts.objective == "Improve the queue."
    assert len(state.run.facts.evaluator_digest) == DIGEST_LENGTH


@pytest.mark.regression
def test_a_host_timed_by_another_clock_than_its_journal_is_refused(tmp_path: Path) -> None:
    """The log dates its records by the journal's clock and the loop counts on the host's: two
    clocks put the run's seconds on two timelines, and a run met its deadline at once."""
    with pytest.raises(CoreCompositionError, match="timing clock"):
        _open_timed_by(tmp_path, ManualClock(start=1.79e9), VirtualClock(at=0.0))


@pytest.mark.parametrize("withheld", [False, True])
def test_the_profile_capture_note_appears_only_when_the_plan_withholds_profile_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, withheld: bool
) -> None:
    """A profiler is chosen on a run that cannot capture; the note follows the plan alone."""
    set_container_cli_credentials(monkeypatch)
    output = _run_diagnostics(tmp_path, profile_capture_withheld=withheld)
    assert ("framework profile capture is unavailable" in output) is withheld
    assert "cannot capture a profile" not in output


@pytest.mark.parametrize(
    ("client_factory", "journal", "missing"),
    [
        (None, True, "agent_client_factory"),
        (_resumable_client, False, "invocation_store_factory"),
    ],
)
def test_missing_resource_fails_at_composition_naming_it(
    tmp_path: Path,
    client_factory: Callable[..., AgentClientProtocol] | None,
    *,
    journal: bool,
    missing: str,
) -> None:
    with pytest.raises(CoreCompositionError, match=missing) as raised:
        _open(tmp_path, client_factory=client_factory, journal=journal)()
    assert raised.value.resource == missing


def test_a_fallback_that_is_the_providers_own_fails_at_startup_naming_the_key(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "model": {"name": "claude-sonnet-5"},
            "agent": {"quota": {"fallback_provider": "claude", "fallback_model": "claude-opus-5"}},
        }
    )

    with pytest.raises(ValueError, match=r"agent\.quota\.fallback_provider"):
        _open(tmp_path, client_factory=_resumable_client, journal=True, config=config)()


def _built_clients(tmp_path: Path, config: Config) -> list[_GateRecordingClient]:
    built: list[_GateRecordingClient] = []

    def factory(**_kwargs: object) -> _GateRecordingClient:
        built.append(_GateRecordingClient())
        return built[-1]

    _open(tmp_path, client_factory=factory, journal=True, config=config)()
    return built


def test_every_client_the_core_path_builds_gets_the_runs_quota_gate(tmp_path: Path) -> None:
    clients = _built_clients(
        tmp_path, Config.model_validate({"model": {"name": "claude-sonnet-5"}})
    )

    assert clients
    assert all(len(client.gates) == 1 for client in clients)


def _one_stop() -> CapacityStops:
    stops = CapacityStops(SimThreads())
    stops.count = 1
    return stops


def test_the_quota_policy_of_the_config_reaches_the_gate(tmp_path: Path) -> None:
    """Under ``fail`` a quota stop ends the turn at once, where ``pause`` would hold it."""
    config = Config.model_validate(
        {"model": {"name": "claude-sonnet-5"}, "agent": {"quota": {"policy": "fail"}}}
    )
    [client] = _built_clients(tmp_path, config)
    [gate] = client.gates
    error = AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, "limit", None)

    with pytest.raises(AgentQuotaError):
        gate.wait_for_capacity(
            error,
            AgentTurnRequest(message="m", label=None, invocation_id=None),
            role="r",
            stops=_one_stop(),
        )


# Every `[agent]` key and a value that differs from its default. The key set is compared
# with the config model, so a key added or removed must be classified here; `quota` is
# consumed by the gate tests above, the rest show up in the specs the run resolves.
_AGENT_KEY_SAMPLES: dict[str, object] = {
    "backend": "stub",
    "cli_provider": "claude",
    "cli_timeout": 17,
    "env_passthrough": ("SOME_TOKEN",),
    "quota": {"policy": "fail"},
    "roles": {"implementer": {"model": "gpt-5.6-luna"}},
    "scripted_seed": 7,
}


def test_every_agent_config_key_is_classified_as_consumed() -> None:
    assert set(_AGENT_KEY_SAMPLES) == set(AgentCfg.model_fields)


@pytest.mark.parametrize("key", [key for key in _AGENT_KEY_SAMPLES if key != "quota"])
def test_a_changed_agent_config_key_changes_the_specs_the_run_resolves(key: str) -> None:
    role = AgentRole(id="implementer", system_prompt="Implement.")

    def specs(config: Config) -> object:
        return resolve_agent_specs(config, (role,), backend=None, provider=None)

    # Without [model].name each provider runs its own default model.
    base = Config()
    changed = Config.model_validate({"agent": {key: _AGENT_KEY_SAMPLES[key]}})

    assert specs(changed) != specs(base)


def test_a_fallback_a_core_run_cannot_switch_to_fails_at_startup_naming_the_key(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "model": {"name": "claude-sonnet-5"},
            "agent": {"quota": {"fallback_provider": "codex", "fallback_model": "gpt-5.5"}},
        }
    )

    with pytest.raises(ValueError, match=r"agent\.quota\.fallback_provider"):
        _open(tmp_path, client_factory=_resumable_client, journal=True, config=config)()


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"plan": None}, "core.plan"),
        ({"retention_label": ""}, "core.retention_label"),
        ({"prompt_templates": "missing-directory"}, "core.prompt_templates"),
        ({"artifact_directories": ("../escape",)}, "core.artifact_directories[0]"),
        ({"artifact_directories": ("/absolute",)}, "core.artifact_directories[0]"),
    ],
)
def test_registration_rejects_an_invalid_core_policy_naming_the_key(
    tmp_path: Path, change: dict[str, object], key: str
) -> None:
    broken = replace(_policy(tmp_path), **change)
    plugin = OrchestrationPlugin(id="core-test", agents=(), options=EmptyOptions, core=broken)
    with pytest.raises(ValueError, match=key.replace("[", r"\[").replace("]", r"\]")):
        OrchestrationRegistration(plugin)


def test_registration_rejects_a_duplicate_operation_role(tmp_path: Path) -> None:
    [operation, *_] = _policy(tmp_path).operations
    broken = replace(_policy(tmp_path), operations=(operation, operation))
    plugin = OrchestrationPlugin(id="core-test", agents=(), options=EmptyOptions, core=broken)
    with pytest.raises(ValueError, match=r"core\.operations\[1\]"):
        OrchestrationRegistration(plugin)


def test_a_plugin_without_a_core_policy_is_refused() -> None:
    with pytest.raises(TypeError, match="core"):
        OrchestrationPlugin(id="neither", agents=(), options=EmptyOptions)  # ty: ignore[missing-argument]


def test_run_plugin_drives_a_core_policy_through_the_shell(tmp_path: Path) -> None:
    """The loop runs a core plugin to its terminal record, and the session sees the outcome."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    clock = VirtualClock(at=0.0)
    integration = LocalRunIntegration(
        clock=clock,
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    request = _request(project_root).model_copy(update={"run_id": "core-run"})
    plugin = replace(
        _plugin(templates),
        core=_policy(
            templates,
            SkeletonStrategy.failing(),
        ),
    )

    async def exercise() -> RunStatus:
        end = await run_plugin(
            request,
            integration,
            plugin,
            EmptyOptions(),
            agent_client_factory=_resumable_client,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
            stop_timer=clock.sleep,
            timing=RunTiming(clock, OsThreads()),
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            profiler_preflight=FakeProfilerPreflight(),
        )
        return end.status

    try:
        status = run_virtual(clock, exercise())
    finally:
        integration.close()

    # The scenario stops the run with a failure result: terminal, and not a success.
    assert status is RunStatus.FAILED
    logged = fold_log(
        read_committed_records(Project.run_log_path_for(project_root, "core-run")), None
    )
    assert logged is not None
    run = logged.state.run
    assert run.result is not None
    assert (run.status.value, run.result.outcome) == ("terminal", "failure")


@pytest.mark.regression
@given(texts=st.lists(st.text(max_size=24), max_size=5))
@example(texts=["live"])
@example(texts=["\x85"])
def test_core_host_tees_startup_and_live_events(texts: list[str]) -> None:
    with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as environ:
        root = Path(directory)
        environ.setenv("VIBESYS_STATE_HOME", str(root / "state"))
        _open(root, client_factory=_resumable_client, journal=True, emitted=tuple(texts))()
        project_root = root / "project"
        (run_id,) = Project.logged_run_ids_for(project_root)
        journal = Project.event_journal_path_for(project_root, run_id).read_text().split("\n")[:-1]
        with Project.run_log_path_for(project_root, run_id).open("rb") as stream:
            records = list(RunLogReader(stream))
        assert records[0].body is not None
        assert records[0].body.field == "epoch_started"
        assert any(
            record.body is not None and record.body.field == "lifecycle" for record in records
        )
        assert all(
            record.body is not None and record.body.field != "legacy_event" for record in records
        )
        notices = [
            StringValue.from_binary(record.body.value.detail.value).value
            if record.body.value.detail is not None
            else record.body.value.message
            for record in records
            if record.body is not None and record.body.field == "notice"
        ]
        if texts:
            assert notices[-len(texts) :] == texts
            assert [json.loads(line)["text"] for line in journal[-len(texts) :]] == texts
