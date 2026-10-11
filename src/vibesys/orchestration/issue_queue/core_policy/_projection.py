"""Read model of an issue-queue run: the strategy state in the shape frontends already consume."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.run.contracts import PluginProjection

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
    return PluginProjection(payload=payload, experiment_revision=experiment_revision)
