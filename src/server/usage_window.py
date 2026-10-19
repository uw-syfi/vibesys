"""What runs spent in a window of time: the pure aggregation behind ``usage.window``.

``usage_window`` takes each run's usage records (``server.run_usage``: one ``UsageRecord`` per
finished agent turn, the one definition of what a turn cost) and the times its kept ideas landed
(``IdeaPoint.at`` of a ``kept`` point, ``server.host_run_dashboard``), and sums what the Usage page
shows. It re-derives nothing: a cost is a record's ``cost_usd``, a best is a kept point. It opens
no file and reads no clock: ``until`` is "now" and arrives as data, so any answer is a function of
its arguments.

A turn is placed at the time it ended and belongs to the window when ``since <= ended_at < until``;
a landing time is placed the same way. Sums use ``math.fsum``, so an answer does not depend on the
order of runs or of records.

Light: the standard library, Pydantic and the helper's contract modules.

Public surface: ``RunUsage``, ``usage_window``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from server.host_run_summary import (
    NO_TOKENS,
    RATE_WINDOW_S,
    USAGE_WINDOW_MAX_AGENTS,
    AgentSpend,
    RunSpend,
    TokenTally,
    UsageRecord,
    UsageWindowReply,
    UsageWindowRequest,
    add_tokens,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from server.host_envelope import RunId


@dataclass(frozen=True)
class RunUsage:
    """One run's facts for a window: who it is, what its turns spent, and when its bests landed."""

    run_id: RunId
    task: str | None
    records: Sequence[UsageRecord]
    bests: Sequence[datetime]
    """When each ``kept`` idea of the run landed, in any order."""


def usage_window(
    request: UsageWindowRequest, runs: Sequence[RunUsage], missing: Sequence[RunId]
) -> UsageWindowReply:
    """The reply to ``request`` over ``runs`` (each named once); ``missing`` are the runs not found."""
    since, until = request.since, request.until
    rate_from = max(since, until - timedelta(seconds=RATE_WINDOW_S))
    placed = [
        (run, record, record.ended_at)
        for run in runs
        for record in run.records
        if record.ended_at is not None and since <= record.ended_at < until
    ]
    run_costs: dict[RunId, list[float]] = {}
    agent_costs: dict[tuple[str | None, str | None, str | None], list[float]] = {}
    agent_turns: dict[tuple[str | None, str | None, str | None], int] = defaultdict(int)
    run_unpriced: dict[RunId, int] = defaultdict(int)
    agent_unpriced: dict[tuple[str | None, str | None, str | None], int] = defaultdict(int)
    for run, record, _ in placed:
        costs = [] if record.cost_usd is None else [record.cost_usd]
        run_costs.setdefault(run.run_id, []).extend(costs)
        agent = (record.role, record.harness, record.model)
        agent_costs.setdefault(agent, []).extend(costs)
        agent_turns[agent] += 1
        if record.cost_usd is None:
            run_unpriced[run.run_id] += 1
            agent_unpriced[agent] += 1
    priced = sum(record.cost_usd is not None for _, record, _ in placed)
    spent = math.fsum(cost for costs in run_costs.values() for cost in costs)
    recent = math.fsum(
        record.cost_usd
        for _, record, ended in placed
        if record.cost_usd is not None and ended >= rate_from
    )
    new_bests = sum(since <= landed < until for run in runs for landed in run.bests)
    tasks = {run.run_id: run.task for run in runs}
    by_run = sorted(
        (
            RunSpend(
                run_id=run_id,
                task=tasks[run_id],
                cost_usd=math.fsum(costs),
                unpriced_turns=run_unpriced[run_id],
            )
            for run_id, costs in run_costs.items()
        ),
        key=lambda each: (-each.cost_usd, each.run_id),
    )
    agents = sorted(
        (
            AgentSpend(
                role=agent[0],
                harness=agent[1],
                model=agent[2],
                cost_usd=math.fsum(costs),
                turns=agent_turns[agent],
                unpriced_turns=agent_unpriced[agent],
            )
            for agent, costs in agent_costs.items()
        ),
        key=lambda each: (-each.cost_usd, each.role or "", each.harness or "", each.model or ""),
    )
    return UsageWindowReply(
        request_id=request.request_id,
        since=since,
        until=until,
        spent_usd=spent,
        spend_per_hour_usd=recent / ((until - rate_from).total_seconds() / 3600),
        tokens=_tokens(record for _, record, _ in placed),
        turns=len(placed),
        unpriced_turns=len(placed) - priced,
        untimed_turns=sum(record.ended_at is None for run in runs for record in run.records),
        new_bests=new_bests,
        cost_per_new_best_usd=None if new_bests == 0 else spent / new_bests,
        by_run=tuple(by_run),
        by_agent=tuple(agents[:USAGE_WINDOW_MAX_AGENTS]),
        agents_omitted=max(0, len(agents) - USAGE_WINDOW_MAX_AGENTS),
        missing_runs=tuple(missing),
    )


def _tokens(records: Iterable[UsageRecord]) -> TokenTally:
    total = NO_TOKENS
    for record in records:
        total = add_tokens(total, record.tokens)
    return total
