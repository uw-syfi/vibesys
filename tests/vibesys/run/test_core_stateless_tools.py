"""Assumption A3 of the strategy port design: a core session can be offered a stateless tool.

`multi`, `evolve` and `issue_queue` give a role the profiler diagnostics server, an MCP server
that analyses captured profiles and holds no run state. Composition admits the bridged
`evaluation` tool and a tool declared `stateless` whose server the host provides
(`_refuse_unbridged_tools`), and refuses every other tool, because a tool that records into run
state would be a second writer beside core. A stateless tool nobody serves is refused too,
never silently dropped.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from tests.support.docker_environment import host_container_backend
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.vibesys.orchestration.plugin import EmptyOptions
from tests.vibesys.run.test_core_host_composition import (
    _policy,
    _request,
    _resumable_client,
    _write_project,
)

from vibesys.api.testing import FakeProfilerPreflight
from vibesys.run.core_services import CoreCompositionError
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api.testing import FakeAgentInvocationStore
from vs_mcp.api import StdioServerDescriptor
from vs_runtime.api import AgentRole, AgentTool, OrchestrationPlugin
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

SERVER = StdioServerDescriptor(name="profiler", command="true")


def _compose(
    tmp_path: Path,
    tool: str,
    *,
    stateless: bool = False,
    served: bool = False,
    declined: bool = False,
) -> None:
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    _write_project(project_root)
    plugin = OrchestrationPlugin(
        id="core-test",
        agents=(
            AgentRole(
                id="implementer",
                system_prompt="Implement.",
                extra_tools=(AgentTool(id=tool, stateless=stateless),),
            ),
        ),
        options=EmptyOptions,
        core=_policy(templates),
    )
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)

    async def exercise() -> None:
        try:
            async with open_product_core_host(
                _request(project_root),
                integration,
                plugin=replace(plugin),
                options=EmptyOptions(),
                timing=RunTiming(clock, 60.0, OsThreads()),
                agent_client_factory=_resumable_client,
                backend_factory=host_container_backend,
                invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
                stateless_tool_servers=(
                    {tool: lambda _context: SERVER}
                    if served
                    else {tool: lambda _context: None}
                    if declined
                    else None
                ),
                stop_timer=FakeStopTimer(threads=OsThreads()),
                blocking=ThreadBlockingRunner(),
                network=UnixNetwork(),
                profiler_preflight=FakeProfilerPreflight(),
            ):
                return
        finally:
            integration.close()

    run_virtual(clock, exercise())


def test_a_role_with_a_served_stateless_tool_composes(tmp_path: Path) -> None:
    _compose(tmp_path, "profiler", stateless=True, served=True)


def test_a_stateless_tool_nobody_serves_is_refused_not_silently_dropped(tmp_path: Path) -> None:
    with pytest.raises(CoreCompositionError, match="no server") as raised:
        _compose(tmp_path, "profiler", stateless=True, served=False)
    assert raised.value.resource == "agent role 'implementer' tool 'profiler'"


def test_a_stateless_tool_its_provider_declines_for_the_run_is_left_out(tmp_path: Path) -> None:
    """A run with no profiler has no profiler tool; the roles that declare it still compose."""
    _compose(tmp_path, "profiler", stateless=True, declined=True)


def test_a_tool_not_declared_stateless_is_refused_even_when_a_server_exists(
    tmp_path: Path,
) -> None:
    with pytest.raises(CoreCompositionError, match="may write run state"):
        _compose(tmp_path, "profiler", stateless=False, served=True)


def test_a_role_with_a_tool_that_writes_run_state_is_refused_naming_it(tmp_path: Path) -> None:
    """The issue board records into legacy state, so core must keep refusing it."""
    with pytest.raises(CoreCompositionError, match="issue-board") as raised:
        _compose(tmp_path, "issue-board")
    assert raised.value.resource == "agent role 'implementer' tool 'issue-board'"
