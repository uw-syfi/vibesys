"""What the operator's `[run]` and `[agent.roles]` settings do to a core run opened by the product host.

The host reads the settings from the config and hands them to the plugin's core policy as facts of
the run (`CoreRunContext`); the policy decides what they mean. This test plays a policy that
records the context it was given and ends only at the deadline it is told, over the product host,
so the wiring from `agent.toml` to the policy and from the deadline to the run's end is the
subject, not any search.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from tests.support.container_credentials import set_container_cli_credentials
from tests.support.core_plugins import core_policy
from tests.support.docker_environment import fake_docker_environment, host_container_backend
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.task_project import write_task
from tests.vibesys.orchestration.plugin import EmptyOptions

from vibesys.api.testing import FakeProfilerPreflight
from vibesys.candidates.api import run_deadline_at
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.events import RunFailureKind
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.core_run import RunEnd, drive_core_run
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import AgentRole, CorePlan, CoreRunContext, OrchestrationPlugin, RunStatus
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock, VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _resumable_client(**_kwargs: object) -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
    )


def _run(
    tmp_path: Path, config: Config, strategy: SkeletonStrategy | None = None
) -> tuple[RunEnd, list[CoreRunContext]]:
    """Open the product host over an idle policy and drive its run to the end."""
    project_root = tmp_path / "project"
    templates = tmp_path / "templates"
    templates.mkdir()
    project_root.mkdir()
    (project_root / "queue.py").write_text("VALUE = 1\n")
    write_task(
        project_root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Improve the queue.\n",
    )
    contexts: list[CoreRunContext] = []
    policy = core_policy(templates, strategy or SkeletonStrategy.idle())

    def plan(context: CoreRunContext) -> CorePlan:
        contexts.append(context)
        deadline = run_deadline_at(context.max_run_seconds)
        return replace(policy.plan(context), deadline_seconds=deadline)

    plugin = OrchestrationPlugin(
        id="core-test",
        agents=(AgentRole(id="implementer", system_prompt="Implement."),),
        options=EmptyOptions,
        core=replace(policy, plan=plan),
    )
    request = RunRequest(
        run_environment=fake_docker_environment(),
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="core-test", config_version=1, options={}),
        config=config,
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="core-test",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    # A paused run asks for nothing; it stays open until the deadline stops it.
    if strategy is None:
        integration.control.request_pause()
    clock = VirtualClock(at=0.0)

    async def exercise() -> RunEnd:
        try:
            async with open_product_core_host(
                request,
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
                return await drive_core_run(host, integration)
        finally:
            integration.close()

    return run_virtual(clock, exercise()), contexts


def test_the_operators_settings_reach_the_policy_and_the_deadline_ends_a_run_that_kept_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One idle run: the deadline and a role's model reach the policy as facts, the deadline ends it."""
    set_container_cli_credentials(monkeypatch)
    config = Config.model_validate(
        {
            "model": {"name": "core-test"},
            "run": {"max_run_seconds": 7},
            "agent": {"roles": {"implementer": {"model": "builder-model"}}},
        }
    )

    end, contexts = _run(tmp_path, config)

    assert [item.max_run_seconds for item in contexts] == [7]
    assert [dict(item.role_models) for item in contexts] == [{"implementer": "builder-model"}]
    assert end.status is RunStatus.FAILED
    assert end.failure is not None
    assert end.failure.kind is RunFailureKind.DEADLINE
    assert end.failure.reason.startswith("deadline")
    assert (end.failure.workstreams_started, end.failure.candidates_kept) == (0, 0)


def test_a_run_that_spent_its_attempt_budget_keeping_nothing_fails_as_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_container_cli_credentials(monkeypatch)
    config = Config.model_validate({"model": {"name": "core-test"}})

    end, _contexts = _run(tmp_path, config, SkeletonStrategy.abandoning())

    assert end.status is RunStatus.FAILED
    assert end.failure is not None
    assert end.failure.kind is RunFailureKind.BUDGET_EXHAUSTED
    assert (end.failure.workstreams_started, end.failure.candidates_kept) == (1, 0)
    assert end.failure.workstream_budget == 1
