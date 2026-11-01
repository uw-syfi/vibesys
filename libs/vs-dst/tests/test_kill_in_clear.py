"""A kill, a pause or a drain that lands inside a ``clear`` leaves a run that still recovers.

A host that took over from a dead one clears each step the dead host left open: it reaches the far
side, reads its ledger and settles what it found (three awaits in the world's runner). Each await
is a moment (`Moment.IN_CLEAR`) a second interruption can land on, so the run is killed again
with a clear half done, and the operator resumes it. Whatever the interruption, the log must
still hold what recovery promises, read from the run.log and the effects ledger alone:

- a step that has no result is run again, and a step that has one is not: no effect finishes
  twice, and every result in the log is the result of an effect that finished;
- the run ends once (a drain's end is reopened by the Resume, so a drained run ends again), and a run that ended has a result for every step it started;
- the run makes progress: it is never stuck, or it waits on a far side that cannot be reached.

The worlds are the tiny search with a ``clear`` that works, and one whose far side is gone for the
first asks (`ClearFailingWorld`). A drain is the operator's stop landing in the ``clear``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from protobuf import Oneof

# test-isolation: the mutation check replaces the world runner's clear rule, the seam under test
from vs_dst._step_host import _WorldRunner
from vs_dst.api import Action, Interruption, Moment, Outcome, simulate
from vs_runtime.api.step_runner import Adopted, Cleared
from vs_sim.api.testing import examples

from ._tiny import TinyWorld
from .test_failing_clear import FAR, DeadClearWorld, _fields

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_core.api import Request, RequestId
    from vs_dst.api import Interrupted, SimulatedRun
    from vs_proto.api import Record

GIVES_UP_AFTER = 3
"""Asks of a failing ``clear`` before its far side answers again, so the run can end."""


def _tags(records: tuple[Record, ...], field: str) -> list[str]:
    return [message.tag for message in _fields(records, field)]


def _ends(records: tuple[Record, ...]) -> int:
    return sum(isinstance(r.body, Oneof) and r.body.field == "run_ended" for r in records)


def check_recovery(
    interrupted: Interrupted,
    *,
    kill: Interruption,
    drained: bool = False,
    answers_every_step: bool = True,
) -> None:
    """The run-log invariants of a recovered run; raises ``AssertionError`` naming the broken one."""
    where = f"{interrupted.interruption} after {kill}: {interrupted.detail}"
    records, effects = interrupted.log_records, interrupted.effects
    assert interrupted.never_stuck(), f"progress: {interrupted.outcome.value}, {where}"
    assert interrupted.outcome is not Outcome.ERRORED, where
    reopened = drained  # a Resume reopens a drained run
    assert 1 <= _ends(records) <= 1 + reopened, f"one ending: {_ends(records)} ends, {where}"
    if (
        answers_every_step
    ):  # a world that leaves an attempt unanswered legitimately finishes it again
        assert interrupted.every_step_finished_once(), f"a step finished twice, {where}"
    started, finished = _tags(records, "step_started"), set(_tags(records, "step_finished"))
    assert set(started) <= finished, f"a step with no result was not run again, {where}"
    assert finished <= set(effects.finished), f"a result no effect produced, {where}"


def _clear_awaits_of_the_resumed_host(normal: SimulatedRun, first: Interruption) -> int:
    """How many ``clear`` awaits the host that takes over after ``first`` passes."""
    return normal.interrupt(first).effects.clear_awaits


def _worlds() -> dict[str, Any]:
    return {
        "works": lambda: TinyWorld(2),
        "fails": lambda: DeadClearWorld("ensure_workspace", FAR, gives_up_after=GIVES_UP_AFTER),
    }


def _first_kills(normal: SimulatedRun) -> list[Interruption]:
    """Kills that leave a step to clear: an effect in flight (nothing sealed), or a commit not made (a result sealed)."""
    return [
        *normal.interruptions(Action.KILL, Moment.IN_FLIGHT),
        *normal.interruptions(Action.KILL, Moment.BEFORE_COMMIT),
    ]


@settings(max_examples=examples(pr=12, nightly=150), deadline=None)
@given(
    world=st.sampled_from(sorted(_worlds())),
    choice=st.integers(min_value=0, max_value=10_000),
    second=st.sampled_from((Action.KILL, Action.STOP, Action.PAUSE)),
)
def test_an_interruption_in_any_await_of_a_clear_leaves_a_run_that_recovers(
    world: str, choice: int, second: Action
) -> None:
    """Kill, drain or pause the host that takes over at any await of its ``clear``; the run recovers."""
    normal = simulate(_worlds()[world]())
    first = _first_kills(normal)[choice % len(_first_kills(normal))]
    awaits = _clear_awaits_of_the_resumed_host(normal, first)
    if awaits == 0:
        return  # a kill at a commit boundary can leave no step to clear
    point = Interruption(second, Moment.IN_CLEAR, choice % awaits)

    if second is Action.KILL:
        interrupted = normal.interrupt_again(first, point)
    else:
        interrupted = normal.interrupt(first, then=point)

    check_recovery(
        interrupted,
        kill=first,
        drained=second is Action.STOP,
        answers_every_step=world == "works",
    )


def test_a_clear_has_an_await_for_a_kill_to_land_in_after_each_kill_of_a_step_in_flight() -> None:
    """Every step in flight at a kill is cleared by the next host, through awaits a kill can land in."""
    normal = simulate(TinyWorld(2))

    counts = {
        _clear_awaits_of_the_resumed_host(normal, kill)
        for kill in normal.interruptions(Action.KILL, Moment.IN_FLIGHT)
    }

    assert counts
    assert min(counts) >= 3


def _every_case() -> list[tuple[Interruption, Interruption]]:
    normal = simulate(TinyWorld(2))
    return [
        (first, Interruption(Action.KILL, Moment.IN_CLEAR, index))
        for first in _first_kills(normal)
        for index in range(_clear_awaits_of_the_resumed_host(normal, first))
    ]


def _run_every_case() -> int:
    """Run every kill in a clear of the tiny world and count the cases that break an invariant."""
    normal = simulate(TinyWorld(2))
    broken = 0
    for first, kill in _every_case():
        try:
            check_recovery(normal.interrupt_again(first, kill), kill=first)
        except AssertionError:
            broken += 1
    return broken


def test_every_kill_in_every_await_of_a_clear_recovers_in_the_tiny_world() -> None:
    assert _run_every_case() == 0


async def _consuming(self: _WorldRunner, step: Request, tag: RequestId) -> Adopted | Cleared:
    """A broken ``clear``: it takes the sealed result out of the ledger at its first await and puts
    it back at its last, so a kill between loses what the step's effect left, and the effect runs
    again. Only a kill inside the ``clear`` shows it."""
    sealed = self._effects.sealed.pop(tag.root, None)
    for _ in range(3):
        await self._await_in_clear()
    if sealed is None:
        return Cleared()
    self._effects.sealed[tag.root] = sealed
    return Adopted(self._sealed(step, tag))


async def _inventing(self: _WorldRunner, step: Request, tag: RequestId) -> Adopted | Cleared:
    """A broken ``clear`` that adopts a result for a step whose effect sealed none."""
    await self._await_in_clear()
    done = self._effects.sealed.get(tag.root) or next(iter(self._effects.sealed.values()), None)
    if done is None:
        return Cleared()
    self._effects.sealed[tag.root] = done
    return Adopted(self._sealed(step, tag))


@pytest.mark.parametrize("mutant", [_consuming, _inventing])
def test_a_broken_recovery_rule_in_clear_breaks_an_invariant(
    mutant: Callable[[_WorldRunner, Request, RequestId], Awaitable[Adopted | Cleared]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariants catch a ``clear`` that loses a result or invents one (a mutation check)."""
    # test-isolation: the broken rule is swapped in for the production-shaped runner of the world
    monkeypatch.setattr(_WorldRunner, "clear", mutant)

    assert _run_every_case() > 0, f"{mutant!r} is not caught by any kill in a clear"
