"""A product host for a plugin with a ``core`` slot composes every core service."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
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
    QuotaCondition,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_core.api import (
    Capabilities,
    validate_startup,
)
from vs_project.api import OrchestrationDescriptor, Project, StoredEnvelope
from vs_runtime.api import (
    AgentRole,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
    RunStatus,
)
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
        config=config or Config.model_validate({"model": {"name": "core-test"}}),
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
) -> Callable[[], CoreServices]:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)

    async def exercise() -> CoreServices:
        try:
            async with open_product_core_host(
                _request(project_root, config),
                integration,
                plugin=_plugin(templates),
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0, OsThreads()),
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
                return host.services
        finally:
            integration.close()

    return lambda: run_virtual(clock, exercise())


def _run_diagnostics(tmp_path: Path, *, profile_capture_withheld: bool) -> str:
    """The text a run with a chosen profiler reports on its event stream while opening."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)

    async def exercise() -> str:
        try:
            async with open_product_core_host(
                _request(project_root, profiler_kind=ProfilerKind.LINUX_CPU),
                integration,
                plugin=_plugin(templates, profile_capture_withheld=profile_capture_withheld),
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0, OsThreads()),
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
            "model": {"name": "core-test"},
            "agent": {"quota": {"fallback_provider": "claude", "fallback_model": "opus"}},
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
    clients = _built_clients(tmp_path, Config.model_validate({"model": {"name": "core-test"}}))

    assert clients
    assert all(len(client.gates) == 1 for client in clients)


def test_the_quota_policy_of_the_config_reaches_the_gate(tmp_path: Path) -> None:
    """Under ``fail`` a quota stop ends the turn at once, where ``pause`` would hold it."""
    config = Config.model_validate(
        {"model": {"name": "core-test"}, "agent": {"quota": {"policy": "fail"}}}
    )
    [client] = _built_clients(tmp_path, config)
    [gate] = client.gates
    error = AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, "limit", None)

    with pytest.raises(AgentQuotaError):
        gate.wait_for_capacity(
            error,
            AgentTurnRequest(message="m", label=None, invocation_id=None),
            role="r",
            attempt=1,
        )


# Every `[agent]` key and a value that differs from its default. The key set is compared
# with the config model, so a key added or removed must be classified here; `quota` is
# consumed by the gate tests above, the rest show up in the specs the run resolves.
_AGENT_KEY_SAMPLES: dict[str, object] = {
    "backend": "stub",
    "cli_provider": "gemini",
    "cli_timeout": 17,
    "env_passthrough": ("SOME_TOKEN",),
    "quota": {"policy": "fail"},
    "roles": {"implementer": {"model": "role-model"}},
}


def test_every_agent_config_key_is_classified_as_consumed() -> None:
    assert set(_AGENT_KEY_SAMPLES) == set(AgentCfg.model_fields)


@pytest.mark.parametrize("key", [key for key in _AGENT_KEY_SAMPLES if key != "quota"])
def test_a_changed_agent_config_key_changes_the_specs_the_run_resolves(key: str) -> None:
    role = AgentRole(id="implementer", system_prompt="Implement.")

    def specs(config: Config) -> object:
        return resolve_agent_specs(config, (role,), backend=None, provider=None)

    base = Config.model_validate({"model": {"name": "core-test"}})
    changed = Config.model_validate(
        {"model": {"name": "core-test"}, "agent": {key: _AGENT_KEY_SAMPLES[key]}}
    )

    assert specs(changed) != specs(base)


def test_a_fallback_a_core_run_cannot_switch_to_fails_at_startup_naming_the_key(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "model": {"name": "core-test"},
            "agent": {"quota": {"fallback_provider": "codex", "fallback_model": "gpt-5"}},
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
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)
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
            timing=RunTiming(clock, 60.0, OsThreads()),
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
    stored = Project.open(project_root).state_store("core-run").load()
    assert isinstance(stored, StoredEnvelope)
    core = json.loads(stored.payload)["envelope"]["core"]["run"]
    assert (core["status"], core["result"]["outcome"]) == ("terminal", "failure")
