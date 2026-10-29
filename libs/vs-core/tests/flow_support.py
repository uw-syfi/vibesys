"""Shared pieces of the flow tests: a role, a prompt, replies and a run starter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import vs_core.api as core
from vs_core.api.testing import FakeView, FlowRun, Prompt, fake_search

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextlib import AbstractAsyncContextManager

FACTS = core.initial_state().run.facts
SHORT = core.Within(limit=10.0)


class Config(core.Value):
    """The toy flows' configuration."""

    width: int = 2


class Plan(core.Value):
    """What a toy agent answers."""

    n: int


EXCHANGES = core.Exchanges(
    correction="fake/correction.j2",
    resume="fake/resume.j2",
    wait_unrecorded="fake/wait_unrecorded.j2",
)
WORKER = core.Role(
    name="worker",
    title="Worker",
    prompt="fake/worker.j2",
    reply=Plan,
    workspace=None,
    access=core.Access.READ_ONLY,
    turn_time=600.0,
    corrections=1,
    lost_turns=2,
    charge=core.Charge.TURN,
    exchanges=EXCHANGES,
)
"""The one role the toy flows ask: it answers a ``Plan``."""

LENIENT = WORKER.model_copy(update={"name": "lenient", "corrections": 0, "lost_turns": 0})
"""A role core never retries for: its first failure is the flow's."""

ROLES = (WORKER, LENIENT)


async def ask(
    ctx: core.Flow, label: str, role: core.Role = WORKER, *, within: float = 10.0
) -> core.TurnFinished | core.Failed:
    """Ask ``role`` with a prompt whose text is ``label`` (a fresh session each call)."""
    return await ctx.agents.session(role).turn(Prompt(text=label), within=within)


def answer_of(event: object) -> int:
    """The ``n`` of a plan reply, or -1 for anything else."""
    if isinstance(event, core.TurnFinished) and isinstance(event.reply, Plan):
        return event.reply.n
    return -1


def start(
    root: Callable[[core.Flow, Config], Coroutine[Any, Any, core.NoWinner | None]],
    *,
    restarting: bool = False,
    adopting: bool = True,
    view: FakeView | None = None,
) -> FlowRun[Config]:
    """Start a flow search; with ``restarting`` it restarts from JSON after every step."""
    definition = fake_search("flows", Config, ROLES)(root)
    return FlowRun.start(definition, Config(), view=view, restarting=restarting, adopting=adopting)


def open_workspace(
    ctx: core.Flow, name: str
) -> AbstractAsyncContextManager[core.Workspace | core.NotOpened]:
    """``ctx.workspaces.open`` for an exclusive-root workspace of ``name`` on the run's input."""
    return ctx.workspaces.open(
        attempt=core.AttemptId(root=name),
        item=core.ItemId(root=name),
        plan=core.WorkspacePlan(mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
        budget=core.AttemptBudget(),
    )


_OPEN = frozenset({core.SpanStatus.RUNNING, core.SpanStatus.WAITING, core.SpanStatus.PENDING})
_FAILURE_OF = {
    core.SpanStatus.FAILED: core.FailureKind.OUTCOME,
    core.SpanStatus.STOPPED: core.FailureKind.INTERRUPTION,
}


def assert_trace_well_formed(trace: core.RunTrace, now: float) -> None:
    """Check the run trace's shape at run-clock time ``now``.

    Every link names a span of the run, every span comes after its parent, its times are on the clock so far and in order, and its
    status agrees with its end, its failure and what it waits on.
    """
    seen: dict[core.SpanId, core.Span] = {}
    ids = {span.id for span in trace.spans}
    for span in trace.spans:
        links = span.label.links if span.label else ()
        assert all(link.to in ids for link in links), f"{span.id} links to a missing span"
        assert span.id not in seen, f"{span.id} recorded twice"
        if span.parent is not None:
            parent = seen.get(span.parent)
            assert parent is not None, f"{span.id} comes before its parent {span.parent}"
            assert parent.started_at <= span.started_at
        assert span.started_at <= now
        assert (span.status in _OPEN) == (span.ended_at is None), span
        if span.ended_at is not None:
            assert span.started_at <= span.ended_at <= now, span
            assert not span.waiting_on, span
        expected = _FAILURE_OF.get(span.status)
        assert (span.failure.kind if span.failure else None) == expected, span
        assert all(span.started_at <= w.since <= now for w in span.waiting_on), span
        if span.type is core.SpanType.OPERATION:
            assert span.status not in {core.SpanStatus.WAITING, core.SpanStatus.PENDING}, span
            assert not span.attempts, span
        for attempt in span.attempts:
            assert attempt.n <= attempt.of
            assert attempt.ended_at is not None or attempt is span.attempts[-1], span
        seen[span.id] = span


def assert_trace_ended(trace: core.RunTrace) -> None:
    """A run that finished has no span still running, waiting or pending."""
    still = [span.id for span in trace.spans if span.status in _OPEN]
    assert not still, f"spans open after the run finished: {still}"
