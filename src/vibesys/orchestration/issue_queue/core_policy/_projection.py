"""Read model of an issue-queue run: the search's published record in the shape frontends consume."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from vibesys.run.contracts import (
    IterationKind,
    IterationMetric,
    IterationStatus,
    IterationSummary,
    IterationTree,
    PluginProjection,
)
from vs_issue_tracker.api import IssueStatus

if TYPE_CHECKING:
    from vibesys.orchestration.issue_queue.search.api import IssueBrief, QueueSnapshot

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def project_run_snapshot(snapshot: QueueSnapshot, *, experiment_revision: int) -> PluginProjection:
    """Project one published record; `experiment_revision` is the core revision it was read at."""
    payload = {
        "version": 1,
        "round_idx": snapshot.round_idx,
        "cursor": snapshot.cursor,
        "phase": _phase(snapshot),
        "current_issue_id": snapshot.current_issue_id,
        "bootstrap_done": bool(snapshot.issues),
        "issues": [
            {
                "id": issue.id,
                "type": issue.type,
                "status": issue.status,
                "title": issue.title,
                "attempts": issue.attempts,
            }
            for issue in snapshot.issues
        ],
        "performance": [
            {
                "iteration": record.iteration,
                "throughput_trend": record.primary_trend,
                "latency_trend": record.secondary_trend,
                "metrics": _JSON_VALUE.validate_json(record.metrics_json)
                if record.metrics_json
                else {},
                "new_issue_ids": list(record.new_issues),
            }
            for record in snapshot.performance
        ],
    }
    return PluginProjection(
        payload=payload,
        experiment_revision=experiment_revision,
        iterations=_iterations(snapshot),
    )


def _phase(snapshot: QueueSnapshot) -> str | None:
    """``"work"`` while a job is in flight, else None.

    The state machine reported the step of the job (start, claim, work, record, ...). A flow has
    no step phases, so the one fact left is whether a job is running.
    """
    working = snapshot.current_issue_id is not None or snapshot.cursor == "assess"
    return "work" if working else None


def _iterations(snapshot: QueueSnapshot) -> IterationTree:
    """Map the durable board snapshot without borrowing round semantics."""
    root_id = "board:issues"
    issues = tuple(sorted(snapshot.issues, key=lambda issue: issue.id))
    nodes = [
        IterationSummary(
            id=root_id,
            kind=IterationKind.BOARD,
            label="Issue board",
            ordinal=0,
            status=_board_status(snapshot),
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
            status=_issue_status(issue, active=issue.id == snapshot.current_issue_id),
            headline_metrics=(IterationMetric(id="attempts", value=float(issue.attempts)),),
        )
        for issue in issues
    )
    return IterationTree.from_nodes(tuple(nodes))


def _board_status(snapshot: QueueSnapshot) -> IterationStatus:
    if snapshot.cursor == "finished":
        if snapshot.stopped:
            return IterationStatus.CANCELLED
        if snapshot.ending is not None and snapshot.ending.outcome == "failure":
            return IterationStatus.FAILED
        return IterationStatus.COMPLETED
    if snapshot.issues or snapshot.current_issue_id is not None or snapshot.round_idx > 0:
        return IterationStatus.ACTIVE
    return IterationStatus.PENDING


def _issue_status(issue: IssueBrief, *, active: bool) -> IterationStatus:
    status = IssueStatus(issue.status)
    if status is IssueStatus.CLOSED:
        return IterationStatus.COMPLETED
    if status is IssueStatus.BLOCKED:
        return IterationStatus.FAILED
    if active or status is IssueStatus.IN_PROGRESS:
        return IterationStatus.ACTIVE
    return IterationStatus.PENDING
