"""A multi run that resumes on changed code goes on at the next round with its ledger and best.

The run's journal no longer replays (the code differs), so its flows restart from their latest
checkpoint. The root's checkpoint holds what the search knows between rounds, so the search
resumes at the round after the last it finished, ranks the rounds already played with the new
ones, and never plays one again. Each story plays ``rounds`` rounds, changes the code at a point
of the next, and finishes the search.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import (
    IMPLEMENTER,
    JUDGE,
    PLANNER,
    MultiHost,
    config,
    implemented,
    reviewed,
)
from tests.vibesys.orchestration.multi.test_stories import design, implement, judge, selection_of
from tests.vibesys.serial_search._replies import plan_reply

from vibesys.candidates.api import measure_input
from vibesys.orchestration.multi.search.api import (
    OPERATIONS,
    ROLES,
    MultiConfig,
    MultiRound,
    multi,
    play_round,
)
from vibesys.search_spans.api import SPANS, adoption_label
from vibesys.serial_search.api import ROADMAP, RoundDone, SerialRun
from vs_core.api import RequestTurn, Retained, RevisionRef
from vs_core.api.search import INPUT, Flow, NoWinner, TaskStopped, Uncompared, search
from vs_core.api.testing import code_changed
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api import DecisionId
    from vs_core.api.testing import Asked

type _Point = str
_POINTS: tuple[_Point, ...] = ("between", "designing", "implementing", "judging")
"""Where the code changes: after a round ended, or while the next round waits on this agent."""
_CHANGED = code_changed(multi)


def _commit(index: int) -> str:
    return f"c{index:05d}"


def _play_round(host: MultiHost, index: int) -> None:
    design(host, f"h{index}")
    implement(host, candidate=_commit(index))
    judge(host, passed=True)
    host.drain_until_turn(PLANNER)


def _until(host: MultiHost, index: int, point: _Point) -> None:
    """Take round ``index`` to ``point``: the wait of the agent whose turn it is."""
    if point == "between":
        return
    design(host, f"h{index}")
    if point == "designing":
        return
    host.drain_until_turn(IMPLEMENTER)
    if point == "implementing":
        return
    implement(host, candidate=_commit(index))
    host.drain_until_turn(JUDGE)


def _fresh(host: MultiHost, role: str, stale: set[DecisionId]) -> Asked:
    """The open turn of ``role`` the restarted round asked, answering what it waits on first.

    ``stale`` are the turns open at the change: their answers are dropped, so none is answered
    here and none is taken for the restarted round's own.
    """
    for _ in range(50):
        fresh = [item for item in host.run.open if item.decision not in stale]
        turn = next((item for item in fresh if item.label == role), None)
        if turn is not None:
            return turn
        host.answer(next(item for item in fresh if item.until is not None))
    message = f"no fresh {role} turn opened: {host.run.timeline()}"
    raise AssertionError(message)


def _finish(host: MultiHost, index: int, stale: set[DecisionId]) -> None:
    """Play round ``index`` out from its start, then end the search.

    A request in flight at the code change is dropped when its answer arrives, and the round
    restarts from its start (it is a flow that checkpointed its start), so the round is played
    from its designer's turn again, on the turns the restarted round asked.
    """
    host.run.reply_raw(_fresh(host, PLANNER, stale), plan_reply(f"h{index}"))
    host.run.reply(
        _fresh(host, IMPLEMENTER, stale),
        implemented(),
        checkpoint=Retained(revision=RevisionRef.of_git_commit(_commit(index))),
    )
    host.run.reply(_fresh(host, JUDGE, stale), reviewed(passed=True))
    host.drain()


@settings(max_examples=examples(pr=12, nightly=60), deadline=None)
@given(
    throughputs=st.lists(
        st.sampled_from((200.0, 400.0, 600.0)), unique=True, min_size=1, max_size=3
    ),
    point=st.sampled_from(_POINTS),
)
@example(throughputs=[400.0, 200.0], point="between")
def test_a_run_resumed_on_changed_code_continues_at_the_next_round_with_the_same_best(
    throughputs: list[float], point: _Point
) -> None:
    """Regression: the root restarted from its config, so every round was played again from 1."""
    played = len(throughputs)
    values = {_commit(i + 1): value for i, value in enumerate(throughputs)}
    next_round = played + 1
    host = MultiHost.start(
        config(max_rounds=next_round),
        restarting=False,
        throughputs={**values, _commit(next_round): 1.0},
    )
    host.drain_until_turn(PLANNER)
    for index in range(1, played + 1):
        _play_round(host, index)
    ledger_before = host.record
    assert ledger_before is not None
    best_before = host.run.state.live.choice
    _until(host, next_round, point)

    stale = {item.decision for item in host.run.open if isinstance(item.request, RequestTurn)}
    host.change_code(
        _CHANGED
    )  # the code changed: the next event finds a journal that no longer replays
    host.run.advance_to(host.run.view.now_at + 1.0)  # the first event after the change
    _finish(host, next_round, stale)

    record = host.record
    assert record is not None
    # the rounds already played are in the record, none again, and the new one follows them
    numbers = [item.round_number for h in record.hypotheses for item in h.rounds]
    assert numbers == list(range(1, next_round + 1)), numbers
    # the best is the best of every round, the new one included
    best = max(values, key=lambda commit: values[commit])
    kind, commit = selection_of(host)
    assert (kind, commit) == ("retained_candidate", best)
    assert best_before is not None


@search("multi", config=MultiConfig, operations=OPERATIONS, roles=ROLES, owned=ROADMAP, spans=SPANS)
async def _saved_before_snapshots(ctx: Flow, config: MultiConfig) -> NoWinner | Uncompared | None:
    """The multi root as it was before it checkpointed what it knows: only its config."""
    ctx.checkpoint(config)
    ctx.fall_back_to_input(adoption_label(INPUT, None, None))
    run = SerialRun(config.hypotheses, ctx.facts.baseline, ctx.task.terms, await measure_input(ctx))
    ctx.fall_back_to_input(run.leading()[1])
    async with ctx.group(limit=1) as rounds:
        while not run.over(stopping=ctx.stop_requested):
            begin = run.start()
            task = rounds.start(f"{begin.number:06d}", play_round, MultiRound.of(begin, config))
            match await ctx.first(rounds, ctx.stopped):
                case RoundDone() as done:
                    run.finished(done, task.span)
                    ctx.choose(*run.leading())
                case TaskStopped():
                    pass
            ctx.publish(run.snapshot())
    return run.ending()


@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    throughputs=st.lists(
        st.sampled_from((200.0, 400.0, 600.0)), unique=True, min_size=1, max_size=3
    ),
    point=st.sampled_from(_POINTS),
)
@example(throughputs=[400.0, 200.0], point="between")
def test_a_run_saved_before_the_root_checkpointed_its_state_is_rebuilt_from_its_record(
    throughputs: list[float], point: _Point
) -> None:
    """The ledger, the rounds' settlements and their spans hold the history the root lost."""
    played = len(throughputs)
    values = {_commit(i + 1): value for i, value in enumerate(throughputs)}
    next_round = played + 1
    host = MultiHost.begin(
        _saved_before_snapshots,
        config(max_rounds=next_round),
        restarting=False,
        throughputs={**values, _commit(next_round): 1.0},
    )
    host.drain_until_turn(PLANNER)
    for index in range(1, played + 1):
        _play_round(host, index)
    _until(host, next_round, point)

    stale = {item.decision for item in host.run.open if isinstance(item.request, RequestTurn)}
    host.change_code(
        _CHANGED
    )  # code that checkpoints its state, and changed: the journal does not replay
    host.run.advance_to(host.run.view.now_at + 1.0)
    _finish(host, next_round, stale)

    record = host.record
    assert record is not None
    numbers = [item.round_number for h in record.hypotheses for item in h.rounds]
    assert numbers == list(range(1, next_round + 1)), numbers
    best = max(values, key=lambda commit: values[commit])
    assert selection_of(host) == ("retained_candidate", best)
    assert len(set(host.measured)) == len(host.measured), "a revision was measured again"
