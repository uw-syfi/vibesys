"""Every optional contract member a strategy may register has a production owner.

A strategy declares optional operations (command capture) and optional tools (a stateless
diagnostics server). If production wiring forgot to provide what they need, the run would fail
at its first use with a typed refusal. These tests open the production host for a policy that
registers every runtime operation role, and check the catalog offers each one, and that every
role tool a built-in core plugin declares is either the bridged evaluation tool or is served.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from tests.support.docker_environment import host_container_backend
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.vibesys.orchestration.plugin import EmptyOptions
from tests.vibesys.run.test_core_host_composition import (
    _plugin,
    _policy,
    _request,
    _resumable_client,
    _write_project,
)

from launch.composition import STATELESS_TOOL_SERVERS
from vibesys.api import catalog
from vibesys.api.testing import FakeProfilerPreflight
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api.testing import FakeAgentInvocationStore
from vs_core.api import (
    CaptureCommandBase,
    CommandCapture,
    OperationDescriptor,
    OperationRegistration,
    SchemaRef,
)
from vs_runtime.api import CoreOperation
from vs_runtime.api.core import EVALUATION_TOOL_ID, OperationRole, RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.core_services import CoreServices


class _Capture(CaptureCommandBase):
    kind: Literal["test.capture_command"] = "test.capture_command"
    outcome_model: ClassVar[type[CommandCapture]] = CommandCapture


def _capture_registration() -> OperationRegistration:
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind="test.capture_command",
            request_schema=SchemaRef(name="test-capture", version=1),
            outcome_schema=SchemaRef(name="test-capture-outcome", version=1),
            lifecycle=_Capture.model_fields["lifecycle"].default,
            inspect=True,
            cancel=False,
        ),
        request_model=_Capture,
        outcome_model=CommandCapture,
    )


def _services(tmp_path: Path) -> CoreServices:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    plugin = _plugin(templates)
    policy = _policy(templates)
    policy = type(policy)(
        plan=policy.plan,
        operations=(
            *policy.operations,
            CoreOperation(OperationRole.CAPTURE_COMMAND, _capture_registration()),
        ),
        retention_label=policy.retention_label,
        prompt_templates=policy.prompt_templates,
    )
    plugin = type(plugin)(id=plugin.id, agents=plugin.agents, options=EmptyOptions, core=policy)
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
                _request(project_root),
                integration,
                plugin=plugin,
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0, OsThreads()),
                agent_client_factory=_resumable_client,
                backend_factory=host_container_backend,
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            ) as host:
                return host.services
        finally:
            integration.close()

    return run_virtual(clock, exercise())


def test_command_capture_has_a_production_owner(tmp_path: Path) -> None:
    """Regression: `OperationPorts.commands` was never set, so capture was refused in production."""
    services = _services(tmp_path)
    offered = {descriptor.kind for descriptor in services.catalog.offered_operations}
    assert "test.capture_command" in offered


def test_every_stateless_tool_a_builtin_core_role_declares_is_served() -> None:
    """A role may declare only the bridged evaluation tool or a stateless tool with a server."""
    declared = {
        (plugin.id, tool.id, tool.stateless)
        for plugin in (getattr(catalog, name).plugin for name in catalog.__all__)
        if plugin.core is not None
        for role in plugin.agents
        for tool in role.extra_tools
    }
    for plugin_id, tool_id, stateless in declared:
        if tool_id == EVALUATION_TOOL_ID:
            continue
        assert stateless, f"{plugin_id}: tool {tool_id!r} is not declared stateless"
        assert tool_id in STATELESS_TOOL_SERVERS, f"{plugin_id}: tool {tool_id!r} has no server"
