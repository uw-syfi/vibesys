"""Golden snapshots for the issue_queue strategy (orchestration id ``"plain"``):
prompts, board/issue-board files, events.

Drives ``IssueQueueOrchestrator.run(ctx)`` end-to-end (real workspace, git
tracking, progress board, issue board, event journal) against a scripted
:class:`FakeAgentClient`, using ``tests.vibesys.golden.harness.run_scripted``
directly: ``descriptor_from_options`` takes no extra ``orchestration_id``
argument and ``run_scripted``'s generic ``RunRequest``/descriptor shape fits
issue_queue without extra setup (no board pre-seeding needed -- the loop
bootstraps its own first issue). See
``tests/vibesys/loops/issue_queue/test_plain_loop.py`` for the reference
mechanics this suite mirrors (role kinds, response schemas, bootstrap issue
behavior).

Three scenarios cover the drain-and-perf-eval outer loop's main paths:

- ``pass``: bootstrap files issue #1, implementer -> judge PASS closes it,
  perf_eval files nothing -> the run drains cleanly and returns ``True``.
- ``retry_then_pass``: the judge FAILs issue #1's first attempt with
  feedback (issue_queue retries the *same* issue, not a different one --
  ``IssueQueueOrchestrator._drain`` re-fetches the same open issue via
  ``board.next_open()`` until it closes or hits the attempt cap); the
  feedback carries into attempt 2's implementer prompt via
  ``_latest_judge_review``, and attempt 2 PASSes.
- ``perf_eval``: same clean pass as ``pass``, but the perf evaluator
  response carries populated metrics/trends/feedback so its distinctive
  prompt and the resulting perf-eval progress/state writes are captured.
  ``new_issue_ids`` stays empty: filing an issue happens through the real
  issue-board MCP tool during a live agent turn, which the fake client
  never invokes, so a scripted ``new_issue_ids`` value would not correspond
  to any issue actually created on the board.

Board files are split across two roots (investigated, not assumed):

- The project workspace (``run.workspace``, i.e. ``host.workspaces.root.path``)
  holds the canonical, git-tracked ``issues.json`` written by
  ``vs_issue_board.api.IssueBoard``.
- The machine-local run-state directory (outside the project workspace, a
  sibling of ``tmp_path`` per ``isolated_vibesys_state_home`` in
  ``tests/conftest.py``) holds the agent-visible ``progress.md`` plus the
  per-issue markdown mirror ``issues/INDEX.md`` and ``issues/000N-*.md``
  rendered by ``vibesys.loops.issue_queue.render``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.harness import ScriptedRun, run_scripted
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_events_snapshot,
    assert_prompt_snapshot,
    prompt_text,
    read_events,
)

from vibesys.evaluators.perf_reply import (
    IssuePerfEvalResponse,
    LatencyStats,
    LoadLevelMetrics,
    PerfMetrics,
    ThroughputStats,
)
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.issue_queue.orchestration import IssueQueueOptions, descriptor_from_options
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import IssueImplementerResponse
from vibesys.roles.judge import IssueJudgeResponse
from vibesys.schemas import PerfTrend
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import OrchestrationDescriptor, Project

if TYPE_CHECKING:
    from pathlib import Path

_STRATEGY = "issue_queue"
_ORCHESTRATION_ID = "plain"


def _options(**overrides: object) -> IssueQueueOptions:
    values: dict[str, object] = {
        "max_rounds": 1,
        "max_attempts_per_issue": 3,
        "max_issues_per_perf_eval": 3,
    }
    values.update(overrides)
    return IssueQueueOptions.model_validate(values)


def _implementer(
    issue_id: int, summary: str = "Built the inference server."
) -> IssueImplementerResponse:
    return IssueImplementerResponse(
        issue_id=issue_id,
        summary=summary,
        files_touched=["server.py"],
        self_check="ran the accuracy checker locally",
    )


def _judge(issue_id: int, verdict: Verdict, feedback: str = "") -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=issue_id,
        analysis="reviewed the diff and the accuracy checks",
        feedback=feedback,
        verdict=verdict,
        new_issues_filed=[],
    )


def _perf_eval(*, with_metrics: bool = False) -> IssuePerfEvalResponse:
    if not with_metrics:
        return IssuePerfEvalResponse(
            analysis="First benchmark run, no prior iteration to compare against.",
            metrics=PerfMetrics(load_levels=[]),
            evaluator_feedback=[],
            new_issue_ids=[],
            throughput_trend=PerfTrend.IMPROVED,
            latency_trend=PerfTrend.IMPROVED,
        )
    return IssuePerfEvalResponse(
        analysis="Throughput saturates around rate=8; TTFT stays flat below that.",
        metrics=PerfMetrics(
            load_levels=[
                LoadLevelMetrics(
                    target_rate=8.0,
                    actual_rate=7.9,
                    num_requests=100,
                    num_completed=100,
                    num_failed=0,
                    duration=20.0,
                    throughput=ThroughputStats(request_throughput=7.9, token_throughput=1011.2),
                    ttft=LatencyStats(
                        mean_ms=42.0, p50_ms=40.0, p90_ms=55.0, p95_ms=60.0, p99_ms=70.0
                    ),
                    tpot=LatencyStats(
                        mean_ms=9.0, p50_ms=8.5, p90_ms=11.0, p95_ms=12.0, p99_ms=15.0
                    ),
                    total_latency=LatencyStats(
                        mean_ms=900.0, p50_ms=880.0, p90_ms=1000.0, p95_ms=1050.0, p99_ms=1200.0
                    ),
                )
            ]
        ),
        evaluator_feedback=["rate=8 is the saturation point; try rate=16 next iteration"],
        new_issue_ids=[],
        throughput_trend=PerfTrend.IMPROVED,
        latency_trend=PerfTrend.MIXED,
    )


def _descriptor(**overrides: object) -> OrchestrationDescriptor:
    return descriptor_from_options(_options(**overrides))


def test_pass_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    runner.enqueue("implementer", _implementer(1))
    runner.enqueue("judge", _judge(1, Verdict.PASS))
    runner.enqueue("perf_eval", _perf_eval())

    run = run_scripted(
        tmp_path,
        orchestration_id=_ORCHESTRATION_ID,
        descriptor=_descriptor(),
        orchestrator_factory=IssueQueueOrchestrator,
        runner=runner,
    )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="pass", workspace=tmp_path.parent)
    _assert_board_files(run, scenario="pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_retry_then_pass_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    runner.enqueue(
        "implementer",
        _implementer(1, "first attempt: partial server, missing /health"),
        _implementer(1, "second attempt: added /health after judge feedback"),
    )
    runner.enqueue(
        "judge",
        _judge(1, Verdict.FAIL, feedback="Missing /health endpoint; checker cannot verify."),
        _judge(1, Verdict.PASS),
    )
    runner.enqueue("perf_eval", _perf_eval())

    run = run_scripted(
        tmp_path,
        orchestration_id=_ORCHESTRATION_ID,
        descriptor=_descriptor(),
        orchestrator_factory=IssueQueueOrchestrator,
        runner=runner,
    )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="retry_then_pass", workspace=tmp_path.parent)
    _assert_board_files(run, scenario="retry_then_pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "retry_then_pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_perf_eval_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    runner.enqueue("implementer", _implementer(1))
    runner.enqueue("judge", _judge(1, Verdict.PASS))
    runner.enqueue("perf_eval", _perf_eval(with_metrics=True))

    run = run_scripted(
        tmp_path,
        orchestration_id=_ORCHESTRATION_ID,
        descriptor=_descriptor(),
        orchestrator_factory=IssueQueueOrchestrator,
        runner=runner,
    )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="perf_eval", workspace=tmp_path.parent)
    _assert_board_files(run, scenario="perf_eval", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "perf_eval", read_events(run.events_path, workspace=tmp_path.parent)
    )


def _assert_prompt_calls(runner: FakeAgentClient, *, scenario: str, workspace: Path) -> None:
    for call in runner.calls:
        role = f"{call.kind}-{call.round_label}"
        assert_prompt_snapshot(
            _STRATEGY,
            role,
            scenario,
            prompt_text(call.system_prompt, call.user_prompt),
            workspace=workspace,
        )


def _local_run_dir(project_dir: Path, run_id: str) -> Path:
    """The machine-local ``plain`` namespace directory (progress.md, issues/)."""
    project = Project.open(project_dir)
    return project.state.local_namespace(run_id, "plain").external_directory()


def _assert_board_files(run: ScriptedRun, *, scenario: str, workspace: Path) -> None:
    project_dir = run.workspace
    issues_json = project_dir / "issues.json"
    if issues_json.exists():
        assert_board_snapshot(
            _STRATEGY, scenario, "issues.json", issues_json.read_text(), workspace=workspace
        )

    local_dir = _local_run_dir(project_dir, run.run_id)
    progress_path = local_dir / "progress.md"
    if progress_path.exists():
        assert_board_snapshot(
            _STRATEGY, scenario, "progress.md", progress_path.read_text(), workspace=workspace
        )
    issues_dir = local_dir / "issues"
    if issues_dir.exists():
        for issue_file in sorted(issues_dir.rglob("*")):
            if not issue_file.is_file():
                continue
            relative = f"issues/{issue_file.relative_to(issues_dir).as_posix()}"
            assert_board_snapshot(
                _STRATEGY, scenario, relative, issue_file.read_text(), workspace=workspace
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
