"""A product host for a plugin with a ``core`` slot composes every core service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Literal

import pytest
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.vibesys.orchestration.plugin import EmptyOptions, capability_plugin

from vibesys.api._session import run_plugin
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import RunRequest
from vibesys.run.core_run import CoreRunLoopUnavailableError
from vibesys.run.core_services import CoreCompositionError
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
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    AgentRole,
    CoreOperation,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
)
from vs_runtime.api.core import OperationRole
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol

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


def _policy(templates: Path) -> CorePolicy:
    def run_facts(context: CoreRunContext) -> RunFacts:
        return RunFacts(
            objective=context.facts.objective,
            baseline=context.baseline,
            evaluator_digest=context.evaluator_digest,
            workload_digest=context.workload_digest,
            environment_digest=context.environment_digest,
        )

    return CorePolicy(
        strategy=lambda _options: SkeletonStrategy(),
        operations=(CoreOperation(OperationRole.RENDER_ARTIFACTS, _render_registration()),),
        reply_schemas={},
        run_facts=run_facts,
        limits=lambda _context: Limits(),
        deadline_at=lambda _context: 1000.0,
        retention_label="selected",
        prompt_templates=templates,
    )


def _plugin(templates: Path) -> OrchestrationPlugin:
    return OrchestrationPlugin(
        id="core-test",
        agents=(AgentRole(id="implementer", system_prompt="Implement."),),
        options=EmptyOptions,
        core=_policy(templates),
    )


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="core-test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "core-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="core-test",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _open(
    tmp_path: Path,
    *,
    client_factory: Callable[..., AgentClientProtocol] | None,
    journal: bool,
) -> Callable[[], object]:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> object:
        try:
            async with open_product_core_host(
                _request(project_root),
                integration,
                plugin=_plugin(templates),
                options=EmptyOptions(),
                agent_client_factory=client_factory,
                invocation_store_factory=(
                    (lambda _state, _key: FakeAgentInvocationStore()) if journal else None
                ),
            ) as host:
                return host.services
        finally:
            integration.close()

    return lambda: asyncio.run(exercise())


def test_core_host_yields_services_whose_state_starts_the_strategy(tmp_path: Path) -> None:
    services = _open(tmp_path, client_factory=_resumable_client, journal=True)()
    strategy = services.strategy  # type: ignore[attr-defined]
    state = services.state  # type: ignore[attr-defined]
    catalog = services.catalog  # type: ignore[attr-defined]
    offered = Capabilities(lifecycle=frozenset(), operations=catalog.offered_operations)
    assert validate_startup(strategy.declaration, offered).operations == ()
    assert [op.kind for op in catalog.offered_operations] == ["test.render"]
    assert state.run.facts.objective == "Improve the queue."
    assert len(state.run.facts.evaluator_digest) == DIGEST_LENGTH


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
    journal: bool,  # noqa: FBT001
    missing: str,
) -> None:
    with pytest.raises(CoreCompositionError, match=missing) as raised:
        _open(tmp_path, client_factory=client_factory, journal=journal)()
    assert raised.value.resource == missing


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"strategy": None}, "core.strategy"),
        ({"limits": 3}, "core.limits"),
        ({"retention_label": ""}, "core.retention_label"),
        ({"reply_schemas": {"bad": _Rendered}}, "core.reply_schemas"),
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


@pytest.mark.xfail(
    strict=True,
    raises=CoreRunLoopUnavailableError,
    reason="SW-1 (vs_runtime._core_run) has not landed, so the shell is not driven yet",
)
def test_run_plugin_drives_a_core_policy_through_the_shell(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        await run_plugin(
            _request(project_root),
            integration,
            _plugin(templates),
            EmptyOptions(),
            agent_client_factory=_resumable_client,
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(),
            stop_timer=asyncio.sleep,
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
        )

    try:
        asyncio.run(exercise())
    finally:
        integration.close()
