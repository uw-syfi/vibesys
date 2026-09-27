"""Public runtime capability tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigurationError,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.api.session import _OpenedAgentEnvironment
from vibesys.context import RunSetup, borrow_run_agent_environment
from vibesys.orchestration.runtime import RunContext
from vibesys.run.integration import LocalRunIntegration
from vibesys.sandbox.run_environment import LocalEnvironment, SkyPilotEnvironment
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api import AgentRole, WorkspaceAccess

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.sandbox.run_environment import RunEnvironmentRequest, RunEnvironmentSession


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="three-agent-rounds", config_version=1, options={"rounds": 2}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="team-demo",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _capture_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[RunEnvironmentRequest], list[str]]:
    requests: list[RunEnvironmentRequest] = []
    closed: list[str] = []
    original_open = LocalEnvironment.open
    original_close = _OpenedAgentEnvironment.close

    def open_environment(
        self: LocalEnvironment, request: RunEnvironmentRequest
    ) -> RunEnvironmentSession:
        requests.append(request)
        return original_open(self, request)

    def close_environment(self: _OpenedAgentEnvironment) -> None:
        closed.append(self.run_id)
        original_close(self)

    monkeypatch.setattr(LocalEnvironment, "open", open_environment)
    monkeypatch.setattr(_OpenedAgentEnvironment, "close", close_environment)
    return requests, closed


def test_skypilot_agents_borrow_the_workspace_session() -> None:
    environment = SkyPilotEnvironment.from_options({"profile": "test-cluster"})
    assert environment.config.profile == "test-cluster"
    closed: list[bool] = []
    session = SimpleNamespace(
        view=SimpleNamespace(cli_sandboxed=False),
        close=lambda: closed.append(True),
    )
    context = cast(
        "_RunResources",
        SimpleNamespace(
            environment_request=SimpleNamespace(
                agent_backend="stub", cli_provider="claude", project_path_policy=None
            ),
            run_environment_session=session,
            skill_source_paths=(),
            backend=ComputeBackend.CPU,
            agent_host_resources=(),
        ),
    )
    borrowed = borrow_run_agent_environment(context, agent_backend="stub", cli_provider="claude")
    assert borrowed.session is session
    borrowed.close()
    assert not closed
    with pytest.raises(ConfigurationError, match="must use its configured backend"):
        borrow_run_agent_environment(context, cli_provider="codex")


def test_root_workspace_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            root = ctx.workspaces.root
            original = root.revision
            assert original is not None
            (root.path / "queue.py").write_text("VALUE = 2\n")
            assert "queue.py" in await root.pending_changes()
            changed = await root.snapshot("public runtime candidate")
            assert changed == root.revision
            await root.restore(original)
            assert (root.path / "queue.py").read_text() == "VALUE = 1\n"
            await root.restore(changed)
            assert (root.path / "queue.py").read_text() == "VALUE = 2\n"
            assert await root.retain(changed, label="public-probe") is None
            assert not ctx.workspaces.supports_parallel_candidates
            with pytest.raises(RuntimeError, match="cannot open isolated candidate sandboxes"):
                await ctx.workspaces.create_candidate()

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_root_environment_and_trusted_evaluator_capabilities(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(_request(project_root), integration, setup=RunSetup()) as ctx:
            assert ctx.environment.view.env_kind == "local"
            assert ctx.environment.view_for() is ctx.environment.view
            assert ctx.environment.reference_path
            assert ctx.environment.model_name == "gpt-test"
            assert ctx.environment.profiler_kind is ProfilerKind.NONE
            assert ctx.environment.workspace_sources == ()
            assert isinstance(ctx.environment.skill_source_paths, tuple)
            assert ctx.environment.run_log_path.parent == ctx.environment.log_dir
            assert ctx.environment.candidate_runtime(1, 1) is not None
            await ctx.control.boundary()
            await ctx.control.debug_step("host probe")
            ctx.switch_log("host-probe")
            ctx.log("host capability probe")
            await ctx.environment.reselect_device()
            await ctx.environment.teardown_deployment("unused")
            execution = await ctx.environment.execute("printf host-ok")
            assert execution.exit_code == 0
            assert "host-ok" in execution.output

            accuracy = await ctx.evaluation.accuracy(ctx.workspaces.root)
            assert accuracy.passed
            assert accuracy.receipt is None
            benchmark = await ctx.evaluation.benchmark(ctx.workspaces.root)
            assert not benchmark.executed

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_scoped_workspace_adopts_candidate_and_closes_its_session(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    client = FakeAgentClient(model="scope").enqueue_text("worker", "scoped response")
    role = AgentRole(
        id="worker",
        system_prompt="work in the candidate",
        workspace_access=WorkspaceAccess.READ_WRITE,
    )

    async def exercise() -> None:
        unclosed_candidate = None
        async with RunContext.open(
            _request(project_root),
            integration,
            setup=RunSetup(),
            agent_roles=(role,),
            agent_client_factory=lambda **_kwargs: client,
        ) as ctx:
            # Local worktrees provide a cheap substrate for the generic parallel capability.
            ctx._resources.run_environment_view = replace(  # noqa: SLF001  # LW-030003; This test reads one private attribute to check internal wiring that has no public accessor.
                ctx.environment.view, supports_parallel_candidate_evaluation=True
            )
            assert ctx.workspaces.supports_parallel_candidates
            parent_revision = ctx.workspaces.root.revision
            assert parent_revision is not None
            scoped = await ctx.workspaces.create_candidate(parent_revision)
            assert scoped.id is not None
            assert scoped.path != ctx.workspaces.root.path
            assert ctx.environment.view_for(scoped).env_kind == "local"
            session = await ctx.agents.create_session(role, workspace=scoped)
            assert session.binding.backend == "fake"
            assert session.binding.driver == "fake"
            assert session.binding.provider == "fake"
            assert session.binding.model == "scope"
            assert await session.turn("work in the fork") == "scoped response"
            (scoped.path / "queue.py").write_text("VALUE = 3\n")
            assert "queue.py" in await scoped.pending_changes()
            revision = await scoped.snapshot("scoped candidate")
            assert scoped.revision == revision
            assert "VALUE = 3" in await ctx.workspaces.export_patch(revision)
            await scoped.restore(parent_revision)
            assert (scoped.path / "queue.py").read_text() == "VALUE = 1\n"
            await scoped.restore(revision)
            assert (scoped.path / "queue.py").read_text() == "VALUE = 3\n"
            assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 1\n"
            await scoped.discard()
            await scoped.discard()
            assert client.closed
            with pytest.raises(ValueError, match="closed"):
                _ = scoped.path
            await ctx.workspaces.adopt(revision)
            assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 3\n"
            unclosed_candidate = await ctx.workspaces.create_candidate(revision)
        assert unclosed_candidate is not None
        with pytest.raises(ValueError, match="closed"):
            _ = unclosed_candidate.path

    try:
        asyncio.run(exercise())
    finally:
        integration.close()
