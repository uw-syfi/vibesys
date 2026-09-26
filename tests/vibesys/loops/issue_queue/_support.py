"""Shared fixtures for the issue_queue (``plain``) orchestrator test suite.

Golden coverage (``tests/vibesys/golden/test_issue_queue_golden.py``) already
drives the clean pass / retry-then-pass / perf-eval-with-metrics scenarios
through ``tests.vibesys.golden.harness.run_scripted`` and snapshots prompts,
board files, and events. This module extends the same fake-seam pattern
(``agent_client_factory`` / ``backend_factory`` injected into
``run_orchestration``, never ``unittest.mock.patch`` on collaborators) with
the one thing ``run_scripted`` does not support: resuming a run against the
same on-disk project across two scripted calls, needed for this directory's
resume/crash-recovery and budget-change tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import patch  # test-isolation: path constants patched below

import pytest
from tests.vibesys.golden.harness import (
    _fake_backend_factory,
    _SharedFakeClient,
    write_minimal_input_bundle,
)

from vibesys.config import Config, as_config
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.evaluators.perf_reply import IssuePerfEvalResponse, PerfMetrics
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.issue_queue.orchestration import IssueQueueOptions, descriptor_from_options
from vibesys.loops.issue_queue.state import IssueQueueStateStore
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.roles.implementer import IssueImplementerResponse
from vibesys.roles.judge import IssueJudgeResponse
from vibesys.run.integration import LocalRunIntegration
from vibesys.schemas import PerfTrend
from vs_issue_tracker.api import IssueBoard, IssueType
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.roles.common import Verdict
    from vs_agent.api import AgentClientProtocol
    from vs_agent.api.testing import FakeAgentClient, FakeInvocation
    from vs_project.api import OrchestrationDescriptor


@dataclass(frozen=True, slots=True)
class PlainRun:
    """A completed (or resumed) scripted issue_queue run."""

    result: bool
    project_dir: Path
    run_id: str


def issue_queue_options(**overrides: object) -> IssueQueueOptions:
    """Build strict plain-loop options with test-friendly defaults."""
    values: dict[str, object] = {
        "max_rounds": 1,
        "max_attempts_per_issue": 3,
        "max_issues_per_perf_eval": 3,
    }
    values.update(overrides)
    return IssueQueueOptions.model_validate(values)


def _build_request(
    tmp_path: Path,
    descriptor: OrchestrationDescriptor,
    config: Config,
    *,
    exp_name: str,
    resume_from: PlainRun | None,
) -> RunRequest:
    if resume_from is None:
        input_dir = write_minimal_input_bundle(tmp_path)
        bundle = load_input_bundle(input_dir)
        return RunRequest(
            project_root=bundle.root,
            orchestration=descriptor,
            config=config,
            input_bundle=bundle,
            objective=bundle.objective,
            exp_name=exp_name,
            runs_dir=tmp_path / "exp_env",
        )
    bundle = load_input_bundle(resume_from.project_dir)
    return RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_from.project_dir.name,
        runs_dir=tmp_path / "exp_env",
        resume=ResumeRef(run_id=resume_from.run_id),
    )


async def _execute(
    request: RunRequest, descriptor: OrchestrationDescriptor, runner: object
) -> bool:
    integration = LocalRunIntegration()
    try:
        return await run_orchestration(
            request,
            integration,
            IssueQueueOrchestrator(descriptor),
            agent_client_factory=cast(
                "Callable[..., AgentClientProtocol]",
                lambda **_kwargs: _SharedFakeClient(cast("FakeAgentClient", runner)),
            ),
            backend_factory=_fake_backend_factory,
        )
    finally:
        integration.close()


def run_plain(
    tmp_path: Path,
    runner: FakeAgentClient,
    *,
    descriptor: OrchestrationDescriptor | None = None,
    exp_name: str = "plain-test",
    resume_from: PlainRun | None = None,
) -> PlainRun:
    """Run (or resume) the ``plain`` orchestrator once with a scripted client.

    A fresh call (``resume_from=None``) provisions a new project under
    ``tmp_path / "exp_env"``. Pass the ``PlainRun`` a prior call returned as
    ``resume_from`` to resume that same on-disk project (input bundle,
    project root, and run id all point back at it), exercising
    resume-after-crash / budget-change behavior.
    """
    descriptor = descriptor or descriptor_from_options(issue_queue_options())
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = _build_request(
        tmp_path, descriptor, config, exp_name=exp_name, resume_from=resume_from
    )

    # test-isolation: redirects a module-level path constant that has no injection seam.
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = asyncio.run(_execute(request, descriptor, runner))

    project_dir = resume_from.project_dir if resume_from else _sole_project_dir(tmp_path)
    run_id = _sole_run_id(project_dir)
    return PlainRun(result=result, project_dir=project_dir, run_id=run_id)


def run_plain_expect_crash(
    tmp_path: Path,
    runner: FakeAgentClient,
    error: type[BaseException],
    *,
    descriptor: OrchestrationDescriptor | None = None,
    exp_name: str = "plain-test",
) -> PlainRun:
    """Run once, expecting ``error`` mid-flight (via ``runner.fail(...)``).

    Simulates a killed process without ``unittest.mock.patch``: the crash is
    injected through ``FakeAgentClient.fail``'s real seam, exactly like a
    real agent-turn failure would surface. Returns a ``PlainRun`` (``result``
    is meaningless) pointing at the project/run the crash left on disk, for a
    follow-up ``run_plain(..., resume_from=...)`` call.
    """
    descriptor = descriptor or descriptor_from_options(issue_queue_options())
    config = as_config(Config.model_validate({"model": {"name": "claude-golden-test"}}))
    request = _build_request(tmp_path, descriptor, config, exp_name=exp_name, resume_from=None)

    with (
        # test-isolation: redirects a module-level path constant that has no injection seam.
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        pytest.raises(error),
    ):
        asyncio.run(_execute(request, descriptor, runner))

    project_dir = _sole_project_dir(tmp_path)
    run_id = _sole_run_id(project_dir)
    return PlainRun(result=False, project_dir=project_dir, run_id=run_id)


def perf_eval_files_issue_callback(
    fake: FakeAgentClient, *, issue_type: IssueType = IssueType.BUG, times: int = 1
) -> Callable[[FakeInvocation], None]:
    """Simulate ``times`` perf_eval turns filing an issue through the real
    board tool, then go quiet so the board can drain.

    The fake client never invokes MCP tools, so a scripted
    ``new_issue_ids`` value never lands on the board (see
    ``tests/vibesys/golden/test_issue_queue_golden.py``'s ``perf_eval``
    docstring). Registered with ``FakeAgentClient.on_invoke``, this creates
    the issue directly against the run's real, on-disk ``IssueBoard`` on the
    first ``times`` ``perf_eval`` turns, the same effect a live issue-board
    MCP call would have.
    """

    def _file(call: FakeInvocation) -> None:
        if call.kind != "perf_eval":
            return
        if len(fake.calls_for("perf_eval")) > times:
            return
        board = IssueBoard(call.workspace / "issues.json")
        board.create(
            type=issue_type,
            title="Filed by perf_eval",
            description="Improve the candidate",
            created_by="perf_eval",
            iteration=len(fake.calls_for("perf_eval")),
        )

    return _file


def _sole_project_dir(tmp_path: Path) -> Path:
    projects = [p for p in (tmp_path / "exp_env").iterdir() if p.is_dir()]
    assert len(projects) == 1, f"expected exactly one project directory, found {projects}"
    return projects[0]


def _sole_run_id(project_dir: Path) -> str:
    runs = Project.open(project_dir).state.list_runs()
    assert len(runs) == 1, runs
    return runs[0].run_id


def plain_state_store(project_dir: Path, run_id: str) -> IssueQueueStateStore:
    """Open the plain-loop typed state adapter for an already-run project."""
    project = Project.open(project_dir)
    return IssueQueueStateStore(project.state.portable_namespace(run_id, "plain"))


def plain_local_dir(project_dir: Path, run_id: str) -> Path:
    """The machine-local ``plain`` namespace directory (progress.md, issues/)."""
    project = Project.open(project_dir)
    return project.state.local_namespace(run_id, "plain").external_directory()


def implementer_response(
    issue_id: int, summary: str = "Built the inference server."
) -> IssueImplementerResponse:
    return IssueImplementerResponse(
        issue_id=issue_id,
        summary=summary,
        files_touched=["server.py"],
        self_check="ran the accuracy checker locally",
    )


def judge_response(issue_id: int, verdict: Verdict, feedback: str = "") -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=issue_id,
        analysis="reviewed the diff and the accuracy checks",
        feedback=feedback,
        verdict=verdict,
        new_issues_filed=[],
    )


def perf_eval_response(new_issue_ids: list[int] | None = None) -> IssuePerfEvalResponse:
    return IssuePerfEvalResponse(
        analysis="Benchmarked.",
        metrics=PerfMetrics(load_levels=[]),
        evaluator_feedback=[],
        new_issue_ids=new_issue_ids or [],
        throughput_trend=PerfTrend.IMPROVED,
        latency_trend=PerfTrend.IMPROVED,
    )
