"""Read model of an issue-queue run: the strategy state in the shape frontends already consume."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.candidates.api import RunPhase
from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    IterationTree,
    PluginProjection,
)
from vs_issue_tracker.api import IssueRow, IssueStatus

if TYPE_CHECKING:
    from vibesys.orchestration.issue_queue.strategy.api import IssueQueueStrategyState


def project_strategy_state(
    state: IssueQueueStrategyState, *, experiment_revision: int
) -> PluginProjection:
    """Project one strategy state; `experiment_revision` is the core revision it was read at."""
    job = state.job
    payload = {
        "version": 1,
        "round_idx": state.round_idx,
        "cursor": state.cursor.value,
        "phase": None if job is None else job.phase.value,
        "current_issue_id": None if job is None or job.issue is None else job.issue.id,
        "bootstrap_done": state.board.bootstrap_done,
        "issues": [
            {
                "id": issue.id,
                "type": issue.type.value,
                "status": issue.status.value,
                "title": issue.title,
                "attempts": issue.attempts,
            }
            for issue in state.board.issues
        ],
        "performance": [
            {
                "iteration": record.iteration,
                "throughput_trend": record.throughput_trend,
                "latency_trend": record.latency_trend,
                "metrics": json.loads(record.metrics_json) if record.metrics_json else {},
                "new_issue_ids": list(record.new_issues),
            }
            for record in state.performance
        ],
    }
    return PluginProjection(
        payload=payload,
        experiment_revision=experiment_revision,
        iterations=_iterations(state),
    )


def _iterations(state: IssueQueueStrategyState) -> IterationTree:
    """Map the durable board snapshot without borrowing round semantics."""
    root_id = "board:issues"
    active_issue_id = None if state.job is None or state.job.issue is None else state.job.issue.id
    issues = tuple(sorted(state.board.issues, key=lambda issue: issue.id))
    nodes = [
        IterationSummary(
            id=root_id,
            kind=IterationKind.BOARD,
            label="Issue board",
            ordinal=0,
            status=_board_status(state),
            child_count=len(issues),
        )
    ]
    nodes.extend(
        IterationSummary(
            id=f"item:{issue.id}",
            parent_id=root_id,
            kind=IterationKind.ITEM,
            label=f"#{issue.id} {issue.title}",
            ordinal=issue.id,
            status=_issue_status(issue, active=issue.id == active_issue_id),
            headline_metrics=(IterationMetric(id="attempts", value=float(issue.attempts)),),
        )
        for issue in issues
    )
    return IterationTree.from_nodes(tuple(nodes))


def _board_status(state: IssueQueueStrategyState) -> IterationStatus:
    if state.phase is RunPhase.FINISHED:
        if state.stopping:
            return IterationStatus.CANCELLED
        if state.ending is not None and state.ending.outcome == "failure":
            return IterationStatus.FAILED
        return IterationStatus.COMPLETED
    if state.board.bootstrap_done or state.job is not None or state.round_idx > 0:
        return IterationStatus.ACTIVE
    return IterationStatus.PENDING


def _issue_status(issue: IssueRow, *, active: bool) -> IterationStatus:
    if issue.status is IssueStatus.CLOSED:
        return IterationStatus.COMPLETED
    if issue.status is IssueStatus.BLOCKED:
        return IterationStatus.FAILED
    if active or issue.status is IssueStatus.IN_PROGRESS:
        return IterationStatus.ACTIVE
    return IterationStatus.PENDING
