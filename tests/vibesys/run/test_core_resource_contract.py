"""Composition-time invariants of the product core resource bundle."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from tests.support.core_plugins import EmptyOptions
from tests.support.real_git import NO_GITHUB

from vibesys.run.core_services import (
    CoreAgentResources,
    CoreCompositionError,
    CoreEnvironment,
    CoreResources,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_core.api import RequestRetries
from vs_project.api import Project
from vs_runtime.api import AgentRole, PollingEvaluationExecutor, RunFacts
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView, TrustedEvaluationPlan
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import InlineBlockingRunner, ManualClock, Sim, SimNetwork

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def _unused_configuration(_role: AgentRole) -> NoReturn:
    message = "invalid resource composition reached agent configuration"
    raise AssertionError(message)


def _unused_session_spec(_role: AgentRole, _workspace: Path) -> NoReturn:
    message = "invalid resource composition reached session configuration"
    raise AssertionError(message)


def _agent_resources(sim: Sim) -> CoreAgentResources:
    return CoreAgentResources(
        client=FakeAgentClient(threads=sim.threads()),
        invocation_slot=FakeAgentInvocationStore(),
        configuration=_unused_configuration,
        session_spec=_unused_session_spec,
    )


def _resources(
    tmp_path: Path,
    sim: Sim,
    *,
    roles: tuple[AgentRole, ...],
    agents: CoreAgentResources | None,
) -> CoreResources:
    workspaces = cast(
        "RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=tmp_path / "workspace"))
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    return CoreResources(
        run_id="resource-contract",
        project=Project.open(project_root),
        facts=RunFacts(
            domain_id="generic",
            objective="Check resource composition.",
        ),
        options=EmptyOptions(),
        workspaces=workspaces,
        evaluation=PollingEvaluationExecutor(FakeEvaluation(), workspaces, clock=ManualClock()),
        environment=CoreEnvironment(
            view=RunEnvironmentView(paths=AgentPaths()),
            evaluation_plan=TrustedEvaluationPlan(),
            evaluation_capacity=1,
            queue_allowance_seconds=1,
            observe_interval_seconds=1,
            observe_backoff_cap_seconds=1,
            request_retries=RequestRetries(limit=1, backoff=1, backoff_cap=1),
            max_run_seconds=None,
            cancellation_bound_seconds=1,
            cleanup_bound_seconds=1,
            reconciliation_bound_seconds=1,
            lifecycle=frozenset(),
        ),
        roles=roles,
        role_models={},
        agents=agents,
        clock=ManualClock(),
        github=NO_GITHUB,
        threads=sim.threads(),
        blocking=InlineBlockingRunner(),
        network=SimNetwork(sim.threads()),
    )


def test_zero_roles_reject_an_open_agent_resource_bundle(tmp_path: Path, sim: Sim) -> None:
    with pytest.raises(CoreCompositionError) as raised:
        _resources(tmp_path, sim, roles=(), agents=_agent_resources(sim))

    assert raised.value.resource == "agent resources"
    assert str(raised.value).endswith("a run with no declared agent roles must not open them")


def test_declared_roles_require_an_agent_resource_bundle(tmp_path: Path, sim: Sim) -> None:
    role = AgentRole(id="implementer", system_prompt="Implement the change.")

    with pytest.raises(CoreCompositionError) as raised:
        _resources(tmp_path, sim, roles=(role,), agents=None)

    assert raised.value.resource == "agent resources"
    assert str(raised.value).endswith("a run with declared agent roles needs session resources")
