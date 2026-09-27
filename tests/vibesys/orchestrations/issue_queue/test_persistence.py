"""On-disk crash and reopen behavior of the issue-queue plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestrations.issue_queue._integration_support import (
    InterruptedTurnError,
    execute,
    interrupted_workspace,
    load_state,
    write_input,
)

from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_issue_tracker.api import IssueBoard
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


_CAPABILITIES = AgentCapabilities(
    session_reuse=True,
    provider_session_resume=True,
    mcp_servers=True,
)


@dataclass(frozen=True, slots=True)
class _InterruptionScenario:
    phase: str
    current_issue_id: int | None
    interrupted_attempts: int
    expected_calls: tuple[int, int, int]


_SCENARIOS = (
    _InterruptionScenario("implementer", 1, 0, (2, 1, 1)),
    _InterruptionScenario("judge", 1, 1, (1, 2, 1)),
    _InterruptionScenario("perf_eval", None, 1, (1, 1, 2)),
)


def _implementation() -> dict[str, object]:
    return {
        "issue_id": 1,
        "summary": "Implemented the inference service.",
        "files_touched": ("server.py",),
        "self_check": "The focused checks passed.",
    }


def _review() -> dict[str, object]:
    return {
        "issue_id": 1,
        "analysis": "The service satisfies the issue.",
        "feedback": "",
        "verdict": "pass",
        "new_issues_filed": (),
    }


def _performance() -> dict[str, object]:
    return {
        "analysis": "The service is measurable.",
        "metrics": {"load_levels": (), "extra": {"requests_per_second": 12.5}},
        "evaluator_feedback": ("Keep the same saturation workload.",),
        "new_issue_ids": (),
        "throughput_trend": "improved",
        "latency_trend": "improved",
    }


def _client(
    role: str,
    response: dict[str, object] | None = None,
    *,
    failure: BaseException | None = None,
) -> FakeAgentClient:
    client = FakeAgentClient(backend_name="stub", capabilities=_CAPABILITIES)
    if failure is not None:
        client.fail(role, failure)
    elif response is not None:
        client.enqueue(role, response)
    return client


def _successful_clients() -> list[FakeAgentClient]:
    return [
        _client("implementer", _implementation()),
        _client("judge", _review()),
        _client("perf_eval", _performance()),
    ]


def _interrupted_clients(phase: str) -> list[FakeAgentClient]:
    interruption = InterruptedTurnError(phase)
    return [
        _client(
            "implementer",
            _implementation() if phase != "implementer" else None,
            failure=interruption if phase == "implementer" else None,
        ),
        _client(
            "judge",
            _review() if phase not in {"implementer", "judge"} else None,
            failure=interruption if phase == "judge" else None,
        ),
        _client(
            "perf_eval",
            failure=interruption if phase == "perf_eval" else None,
        ),
    ]


def _board_summary(project_root: Path) -> tuple[object, ...]:
    return tuple(
        (
            issue.id,
            issue.type,
            issue.title,
            issue.description,
            issue.status,
            issue.attempts,
            issue.created_by,
            tuple(
                (
                    event.actor,
                    event.action,
                    event.iteration,
                    event.note,
                    event.payload,
                )
                for event in issue.history
            ),
        )
        for issue in IssueBoard(project_root / "issues.json").list()
    )


def _call_counts(clients: Sequence[FakeAgentClient]) -> tuple[int, int, int]:
    return (
        sum(len(client.calls_for("implementer")) for client in clients),
        sum(len(client.calls_for("judge")) for client in clients),
        sum(len(client.calls_for("perf_eval")) for client in clients),
    )


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda scenario: scenario.phase)
def test_persisted_reopen_resumes_only_the_interrupted_phase(
    tmp_path: Path,
    scenario: _InterruptionScenario,
) -> None:
    baseline_input = write_input(tmp_path / f"baseline-{scenario.phase}")
    baseline_clients = _successful_clients()
    baseline_status, baseline_id, baseline_workspace = execute(
        baseline_input,
        baseline_clients,
    )
    baseline_state = load_state(baseline_workspace, baseline_id)
    assert baseline_state is not None
    baseline_board = _board_summary(baseline_workspace)

    crashed_input = write_input(tmp_path / f"crashed-{scenario.phase}")
    crashed_clients = _interrupted_clients(scenario.phase)
    with pytest.raises(InterruptedTurnError, match=scenario.phase):
        execute(crashed_input, crashed_clients)

    resumed_workspace, interrupted_id = interrupted_workspace(crashed_input)
    interrupted_state = load_state(resumed_workspace, interrupted_id)
    assert interrupted_state is not None
    assert interrupted_state.bootstrap_done is True
    assert (interrupted_state.round_idx, interrupted_state.phase) == (0, scenario.phase)
    assert interrupted_state.current_issue_id == scenario.current_issue_id
    interrupted_issues = IssueBoard(resumed_workspace / "issues.json").list()
    assert len(interrupted_issues) == 1
    assert interrupted_issues[0].attempts == scenario.interrupted_attempts
    assert [event.action for event in interrupted_issues[0].history].count("create") == 1

    resumed_clients = _successful_clients()
    resumed_status, resumed_id, reopened_workspace = execute(
        resumed_workspace,
        resumed_clients,
        resume_run_id=interrupted_id,
    )

    assert baseline_status is resumed_status is RunStatus.SUCCEEDED
    assert resumed_id == interrupted_id
    assert reopened_workspace == resumed_workspace
    assert load_state(resumed_workspace, resumed_id) == baseline_state
    assert _board_summary(resumed_workspace) == baseline_board
    assert _call_counts((*crashed_clients, *resumed_clients)) == scenario.expected_calls
    assert all(client.closed for client in (*baseline_clients, *crashed_clients, *resumed_clients))

    issues = IssueBoard(resumed_workspace / "issues.json").list()
    assert len(issues) == 1
    issue = issues[0]
    assert issue.attempts == 1
    assert [event.action for event in issue.history].count("create") == 1
    assert [event.action for event in issue.history].count("attempt") == 1
