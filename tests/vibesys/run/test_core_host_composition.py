"""A product host for a plugin with a ``core`` slot composes every core service."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import pytest
from tests.support.container_credentials import set_container_cli_credentials
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.skeleton_strategy import SkeletonState, SkeletonStrategy
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import EmptyOptions, capability_plugin

from vibesys.api._session import run_plugin
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import RunRequest
from vibesys.run.core_services import CoreCompositionError
from vibesys.run.evaluation_backend import semantic_evaluation_identity
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_core.api import (
    Capabilities,
    LifecycleClass,
    Limits,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    RunFacts,
    SchemaRef,
    Value,
    validate_startup,
)
from vs_project.api import OrchestrationDescriptor, Project, StoredEnvelope
from vs_runtime.api import (
    AgentRole,
    CoreOperation,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
    RunStatus,
)
from vs_runtime.api.core import OperationRole, RunTiming
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.run.core_services import CoreServices
    from vs_agent.api import AgentClientProtocol
    from vs_core.api import Strategy

DIGEST_LENGTH = 64


class _Rendered(Value):
    status: Literal["succeeded", "failed"] = "succeeded"


class _Render(OperationRequest):
    kind: Literal["test.render"] = "test.render"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[Value]] = _Rendered
    subject: str


def _render_registration() -> OperationRegistration:
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind="test.render",
            request_schema=SchemaRef(name="test-render", version=1),
            outcome_schema=SchemaRef(name="test-render-outcome", version=1),
            lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
            inspect=True,
            cancel=False,
        ),
        request_model=_Render,
        outcome_model=_Rendered,
    )


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(capabilities=AgentCapabilities(provider_session_resume=True))


def _policy(
    templates: Path,
    strategy: Strategy[Any] | None = None,
    deadline_seconds: float = 1000.0,
    *,
    profile_capture_withheld: bool = False,
) -> CorePolicy:
    def plan(context: CoreRunContext) -> CorePlan:
        identity = semantic_evaluation_identity(
            context.evaluation_plan, context.facts, context.environment
        )
        return CorePlan(
            strategy=SkeletonStrategy() if strategy is None else strategy,
            reply_schemas={},
            facts=RunFacts(
                objective=context.facts.objective,
                baseline=context.baseline,
                evaluator_digest=identity.evaluator.value,
                workload_digest=identity.workload.value,
                environment_digest=identity.environment.value,
            ),
            limits=Limits(),
            deadline_seconds=deadline_seconds,
            profile_capture_withheld=profile_capture_withheld,
        )

    return CorePolicy(
        plan=plan,
        operations=(CoreOperation(OperationRole.RENDER_ARTIFACTS, _render_registration()),),
        retention_label="selected",
        prompt_templates=templates,
    )


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
    integration = LocalRunIntegration()
    clock = VirtualClock(at=0.0)

    async def exercise() -> CoreServices:
        try:
            async with open_product_core_host(
                _request(project_root, config),
                integration,
                plugin=_plugin(templates),
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0),
                agent_client_factory=client_factory,
                backend_factory=host_container_backend,
                invocation_store_factory=(
                    (lambda _state, _key: FakeAgentInvocationStore()) if journal else None
                ),
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
    integration = LocalRunIntegration()
    clock = VirtualClock(at=0.0)

    async def exercise() -> str:
        try:
            async with open_product_core_host(
                _request(project_root, profiler_kind=ProfilerKind.LINUX_CPU),
                integration,
                plugin=_plugin(templates, profile_capture_withheld=profile_capture_withheld),
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0),
                agent_client_factory=_resumable_client,
                backend_factory=host_container_backend,
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                profiler_preflight=lambda kind: ProfilerPreflightResult(kind=kind, usable=True),
            ):
                return "\n".join(repr(event) for event in integration.events.read())
        finally:
            integration.close()

    return run_virtual(clock, exercise())


def test_core_host_yields_services_whose_state_starts_the_strategy(tmp_path: Path) -> None:
    services = _open(tmp_path, client_factory=_resumable_client, journal=True)()
    strategy = services.strategy
    state = services.state
    catalog = services.catalog
    offered = Capabilities(lifecycle=frozenset(), operations=catalog.offered_operations)
    assert validate_startup(strategy.declaration, offered).operations == ()
    assert [op.kind for op in catalog.offered_operations] == ["test.render"]
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
    operation = CoreOperation(OperationRole.RENDER_ARTIFACTS, _render_registration())
    broken = replace(_policy(tmp_path), operations=(operation, operation))
    plugin = OrchestrationPlugin(id="core-test", agents=(), options=EmptyOptions, core=broken)
    with pytest.raises(ValueError, match=r"core\.operations\[1\]"):
        OrchestrationRegistration(plugin)


def test_a_plugin_needs_exactly_one_of_orchestrate_and_core(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one of orchestrate and core"):
        OrchestrationPlugin(id="neither", agents=(), options=EmptyOptions)
    legacy = capability_plugin("legacy")
    with pytest.raises(ValueError, match="exactly one of orchestrate and core"):
        replace(legacy, core=_policy(tmp_path))


def test_run_plugin_drives_a_core_policy_through_the_shell(tmp_path: Path) -> None:
    """The loop runs a core plugin to its terminal record, and the session sees the outcome."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration()
    clock = VirtualClock(at=0.0)
    request = _request(project_root).model_copy(update={"run_id": "core-run"})
    plugin = replace(
        _plugin(templates),
        core=_policy(
            templates,
            SkeletonStrategy(
                state=SkeletonState(schema_version=1, phase="failed", failure="scripted"),
                measured=False,
            ),
        ),
    )

    async def exercise() -> RunStatus:
        end = await run_plugin(
            request,
            integration,
            plugin,
            EmptyOptions(),
            agent_client_factory=_resumable_client,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
            stop_timer=asyncio.sleep,
            timing=RunTiming(clock, 60.0),
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
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
