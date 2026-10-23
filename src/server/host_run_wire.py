"""The run digests and the usage window as the registered reads' messages.

The helper's models (``RunSummary``, ``UsageWindowReply``) stay the one definition of what a
digest or a window says; this module only restates them as the generated messages the gateway
serves. A value the model leaves ``None`` is absent in the message, never a zero.

Public surface: ``digests_response`` and ``usage_response``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from server.host_api import RunDigest, RunDigestUnreadable

if TYPE_CHECKING:
    from datetime import datetime
    from types import ModuleType

    from server.host_api import RunList
    from server.host_run_dashboard import BestReading, IdeaPoint, RunDashboard
    from server.host_run_summary import TokenTally, UsageWindowReply
    from vs_proto.api import host_payloads as pb

__all__ = ["digests_response", "usage_response"]


def _wire() -> ModuleType:
    """Load protobuf contracts only when a migrated read executes, preserving light startup."""
    return import_module("vs_proto.api")


def _timestamp(value: datetime) -> object:
    return _wire().Timestamp.from_datetime(value)


def _enum[Member](kind: type[Member], name: str) -> Member:
    """The member of a generated enum whose name is ``name`` in upper case."""
    return getattr(kind, name.upper())


def _point(point: IdeaPoint) -> pb.IdeaPoint:
    wire = _wire().host_payloads
    result = wire.IdeaPoint(verdict=_enum(wire.IdeaVerdict, point.verdict.value))
    if point.ratio is not None:
        result.ratio = point.ratio
    if point.at is not None:
        result.at = _timestamp(point.at)
    return result


def _best(best: BestReading) -> pb.BestReading:
    result = _wire().host_payloads.BestReading(value=best.value)
    if best.metric is not None:
        result.metric = best.metric
    if best.unit is not None:
        result.unit = best.unit
    return result


def _dashboard(dashboard: RunDashboard, attention: str | None) -> pb.RunDashboard:
    wire = _wire().host_payloads
    result = wire.RunDashboard(
        points=[_point(point) for point in dashboard.points],
        points_omitted=dashboard.points_omitted,
        failed_streak=dashboard.failed_streak,
        attention=_enum(wire.RunAttention, attention or "none"),
    )
    if dashboard.last_event_at is not None:
        result.last_event_at = _timestamp(dashboard.last_event_at)
    if dashboard.best is not None:
        result.best = _best(dashboard.best)
    return result


def _digest(digest: RunDigest, attention: str | None) -> pb.RunDigest:
    wire = _wire().host_payloads
    result = wire.RunDigest()
    if digest.ideas is not None:
        result.ideas = digest.ideas
    if digest.adopted_ratio is not None:
        result.adopted_ratio = digest.adopted_ratio
    if digest.best_so_far_ratio is not None:
        result.best_so_far_ratio = digest.best_so_far_ratio
    if digest.duration_s is not None:
        result.duration_s = digest.duration_s
    if digest.ending is not None:
        ending = digest.ending
        result.ending = wire.RunEnding(
            outcome=(
                _enum(wire.RunEndOutcome, ending.outcome.value.replace("-", "_"))
                if ending.outcome is not None
                else wire.RunEndOutcome.UNSPECIFIED
            ),
            summary=ending.summary,
            uncompared=ending.uncompared,
        )
    if digest.closing is not None:
        result.closing = _enum(wire.RunClosing, digest.closing.value)
    if digest.dashboard is not None:
        result.dashboard = _dashboard(digest.dashboard, attention)
    return result


def digests_response(request_id: str, runs: RunList) -> pb.ListRunDigestsResponse:
    """One entry for each run of ``runs`` whose journal gave a digest, in the order of ``runs``."""
    wire = _wire().host_payloads
    entries = []
    for run in runs.runs:
        digest = run.digest
        if isinstance(digest, RunDigest):
            result = _wire().Oneof("digest", _digest(digest, run.attention))
        elif isinstance(digest, RunDigestUnreadable):
            result = _wire().Oneof("unreadable", digest.message)
        else:
            continue
        entries.append(
            wire.RunDigestEntry(
                run_id=run.run_id, created_at=_timestamp(run.created_at), result=result
            )
        )
    return wire.ListRunDigestsResponse(request_id=request_id, digests=entries)


def _tokens(tally: TokenTally) -> pb.TokenTally:
    result = _wire().host_payloads.TokenTally()
    for name in ("input", "cache_read", "cache_write", "output", "reasoning"):
        value = getattr(tally, name)
        if value is not None:
            setattr(result, name, value)
    return result


def usage_response(request_id: str, reply: UsageWindowReply) -> pb.UsageWindowResponse:
    """The usage window ``reply`` states, restated."""
    wire = _wire().host_payloads
    by_run = []
    for spend in reply.by_run:
        row = wire.RunSpend(
            run_id=spend.run_id, cost_usd=spend.cost_usd, unpriced_turns=spend.unpriced_turns
        )
        if spend.task is not None:
            row.task = spend.task
        by_run.append(row)
    by_agent = []
    for agent in reply.by_agent:
        entry = wire.AgentSpend(
            cost_usd=agent.cost_usd, turns=agent.turns, unpriced_turns=agent.unpriced_turns
        )
        for name in ("role", "harness", "model"):
            value = getattr(agent, name)
            if value is not None:
                setattr(entry, name, value)
        by_agent.append(entry)
    result = wire.UsageWindowResponse(
        request_id=request_id,
        since=_timestamp(reply.since),
        until=_timestamp(reply.until),
        spent_usd=reply.spent_usd,
        spend_per_hour_usd=reply.spend_per_hour_usd,
        tokens=_tokens(reply.tokens),
        turns=reply.turns,
        unpriced_turns=reply.unpriced_turns,
        untimed_turns=reply.untimed_turns,
        new_bests=reply.new_bests,
        by_run=by_run,
        by_agent=by_agent,
        agents_omitted=reply.agents_omitted,
        missing_runs=list(reply.missing_runs),
    )
    if reply.cost_per_new_best_usd is not None:
        result.cost_per_new_best_usd = reply.cost_per_new_best_usd
    return result
