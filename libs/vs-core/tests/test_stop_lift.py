"""A stop is a pause the operator can lift: the run goes on with its history.

An explicit resume lifts a stop that stands (``StopLifted``). The flow runtime then takes the
stop back from every flow it has not yet interrupted, and a flow that already took ``Stopped``
stays ended. Each story runs live and with a restart after every step (``begin``), so the
journal replays both events and the replay tells the same story. Core's own rule, which
accepts the resume exactly while the run has not committed its ending, is told in the second
half.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import vs_core.api as core
from vs_core.api.testing import FAKE_REGISTRY, FakeView

from .flow_support import Config, Plan, answer_of, ask, start
from .test_flows import Begin, answer, ended, stop_aware, waits
from .test_flows_codec import ask_twice

if TYPE_CHECKING:
    from vs_core.api.testing import FlowRun

_ROUNDS = 3


@pytest.fixture(params=[False, True], ids=["live", "restart-every-step"])
def begin(request: pytest.FixtureRequest) -> Begin:
    """Starts a flow; the fixture's two settings are a live run and a restart after every step."""
    restarting = bool(request.param)
    return lambda root, view=None, adopting=True: start(
        root, restarting=restarting, view=view, adopting=adopting
    )


class History(core.Value):
    """The answers a search has collected, in order, and whether a stop cut it short."""

    answers: tuple[int, ...] = ()
    cut_short: bool = False


async def rounds(ctx: core.Flow, _config: Config) -> core.NoWinner:
    """Ask for ``_ROUNDS`` answers one after the other, between which a standing stop ends it."""
    history = History()
    for index in range(_ROUNDS):
        if ctx.stop_requested:
            ctx.publish(history.model_copy(update={"cut_short": True}))
            return core.NoWinner(reason="stopped")
        history = history.model_copy(
            update={"answers": (*history.answers, answer_of(await ask(ctx, f"round-{index}")))}
        )
        ctx.publish(history)
    return core.NoWinner(reason="done")


def _history(run: FlowRun[Config]) -> History:
    found = run.published(History)
    assert found is not None
    return found


def _play(run: FlowRun[Config], *, stop_in: int, lifted: bool) -> None:
    """Answer every round; the stop (and its lift) arrive while round ``stop_in`` is asked."""
    for index in range(_ROUNDS):
        if run.finished is not None:
            return
        asked = run.expect_turn(f"round-{index}")
        if index == stop_in:
            run.stop()
            if lifted:
                run.lift_stop()
        run.reply(asked, Plan(n=index * 10))


@pytest.mark.parametrize("stop_in", range(_ROUNDS))
def test_a_lifted_stop_leaves_the_search_with_every_round_it_would_have_had(
    begin: Begin,
    stop_in: int,
) -> None:
    """Regression: a stop recorded in the run's state could never be taken back by a resume."""
    run = begin(rounds)
    _play(run, stop_in=stop_in, lifted=True)
    assert _history(run) == History(answers=(0, 10, 20))
    assert ended(run).reason == "no-winner: done"
    assert not run.state.live.stop


@pytest.mark.parametrize("stop_in", range(_ROUNDS))
def test_a_stop_that_stands_still_cuts_the_search_short_after_the_round_in_flight(
    begin: Begin,
    stop_in: int,
) -> None:
    run = begin(rounds)
    _play(run, stop_in=stop_in, lifted=False)
    last = stop_in == _ROUNDS - 1
    assert _history(run) == History(
        answers=tuple(10 * i for i in range(stop_in + 1)), cut_short=not last
    )


def test_a_stop_after_a_lift_stands_again(begin: Begin) -> None:
    run = begin(rounds)
    first = run.expect_turn("round-0")
    run.stop()
    run.lift_stop()
    run.stop("stop-2")
    assert run.state.live.stop is not None
    run.reply(first, Plan(n=0))
    assert _history(run) == History(answers=(0,), cut_short=True)


def test_a_lift_with_no_stop_standing_changes_nothing(begin: Begin) -> None:
    run = begin(rounds)
    run.lift_stop()
    _play(run, stop_in=-1, lifted=False)
    assert _history(run) == History(answers=(0, 10, 20))


def test_a_stop_a_task_has_not_yet_taken_is_lifted_with_it(begin: Begin) -> None:
    """A stop that came during a shielded setup is pending: the lift cancels it."""
    run = begin(stop_aware)
    run.stop()  # while both workstreams are still opening
    run.lift_stop()
    answer(run, "open")
    answer(run, "open")
    assert {"a/work", "b/work"} <= set(waits(run))
    answer(run, "work")
    answer(run, "work")
    assert waits(run)[-2:] == ["a/settle", "b/settle"]  # the workstreams settle as done
    answer(run, "settle")
    answer(run, "settle")
    answer(run, "plan", 5)
    reason = ended(run).reason
    assert "a done | b done" in reason


def test_a_task_the_stop_already_ended_stays_ended_after_the_lift(begin: Begin) -> None:
    """Work the stop ended is not brought back: its task ended as stopped."""
    run = begin(stop_aware)
    for asked in list(run.open):
        if asked.prompt is not None and asked.prompt.text == "open":
            run.reply(asked, Plan(n=0))
    run.stop()
    assert waits(run)[-2:] == ["a/settle-stopped", "b/settle-stopped"]
    run.lift_stop()
    answer(run, "settle-stopped")
    answer(run, "settle-stopped")
    answer(run, "plan", 5)
    reason = ended(run).reason
    assert "a done | b done" in reason  # the workstreams returned what they were stopped with
    assert not run.state.live.stop


@pytest.mark.regression
@pytest.mark.parametrize("stop_in", range(_ROUNDS))
def test_a_lift_after_a_stop_ended_a_search_that_never_checkpointed_starts_it_again(
    begin: Begin,
    stop_in: int,
) -> None:
    """Regression: the lift restarted every flow from a snapshot it recorded, and a flow that had
    recorded none made the resume fail as ``FlowCodeChanged`` though no code had changed, so a run
    stopped before its first checkpoint could never be resumed. Its flows start from the argument
    they began with, with the stop taken back."""
    run = begin(rounds)
    for index in range(_ROUNDS):
        asked = run.expect_turn(f"round-{index}")
        if index == stop_in:
            run.stop()
            run.reply(asked, Plan(n=index * 10))
            break
        run.reply(asked, Plan(n=index * 10))
    assert run.finished is not None

    run.lift_stop()

    assert not run.state.live.stop
    for index in range(_ROUNDS):
        run.reply(run.expect_turn(f"round-{index}"), Plan(n=index * 10))
    assert _history(run) == History(answers=(0, 10, 20))


def test_a_run_saved_stopped_by_the_previous_version_resumes_and_lifts() -> None:
    """A saved envelope holds the stop in core's controls and in the flows' state; both lift."""
    saved = Path(__file__).parent / "envelopes" / "v36-stopped.json"
    envelope = FAKE_REGISTRY.decode_envelope(
        core.RunEnvelope[core.FlowState], saved.read_text(encoding="utf-8")
    )
    assert envelope.schema_version == core.ENVELOPE_SCHEMA_VERSION
    assert core.requested_stop_mode(envelope.core.run) is core.StopMode.DRAIN
    assert envelope.strategy.live.stop is not None

    resume = core.ControlInput(
        control_id=core.ControlId(root="resume"), action="resume", when="now"
    )
    transition = core.step(envelope.core, core.RunControlEvent(control=resume, now_at=1.0))
    lifted = next(e for e in transition.events if isinstance(e, core.StopLiftRecorded))
    assert core.requested_stop_mode(transition.state.run) is None

    run = start(ask_twice)
    flows, _ = run.driver.update(
        envelope.strategy, core.StopLifted(control=lifted.control), FakeView().current()
    )
    assert flows.live.stop is None
