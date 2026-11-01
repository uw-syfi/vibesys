"""No agent evaluation tool call can halt the run, and no restart loses or repeats a charge.

The production step loop hosts the skeleton search over a vs-dst world (`world.py`). A scripted
agent calls the real `AgentEvaluationBridge` from its turns with the wire frames the socket
would carry: submits and waits (own, stale, repeated and unknown handles), calls made after a
turn ended with the token it held, replies lost or slower than the agent's deadline, and calls
after the run ended. Then the host is killed at every durable boundary and the operator pauses
the run, and a new host resumes it over the same run.log and the same owed record.

Properties: the run reaches its terminal state and no call raised; every refusal carries
non-empty template text; the budget
refuses a second submission of a candidate only after the agent read the first one's handle,
and one candidate has one handle; every charged submission the agent never read is reported.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.system.agent_tools.world import ROLE, SCOPE, AgentToolWorld, Program, Turn, new_bridge

from vs_core.api import CoreState, RunStatus, SessionPhase, SubmitMeasurement
from vs_dst.api import Action, Moment, SimulatedRun, StepLoopHosting, StopKind, simulate
from vs_runtime.api.core import handle_for
from vs_sim.api.testing import Depth, depth, evenly_spaced, examples

if TYPE_CHECKING:
    from pathlib import Path

STEP_LOOP = StepLoopHosting()
_RUNS = itertools.count()
SAMPLE_CRASHES = 3
SAMPLE_INTERRUPTIONS = 2
"""Points of each class of interruption a pull request tries per example."""


def _world(world: object) -> AgentToolWorld:
    assert isinstance(world, AgentToolWorld)
    return world


def check(run: SimulatedRun) -> AgentToolWorld:
    """The run ended terminally, and no call halted it or went unanswered in words.

    A turn yields a continuation only for a wait the bridge accepted. What core makes of it, and
    the resume of an accepted one, is core's (the wait tests of `vs-core`): the skeleton run
    ends with no winner when core refuses the wait, and still ends.
    """
    world = _world(run.world)
    state = run.core
    assert run.finished, run.halted
    assert state.run.status == RunStatus.TERMINAL
    assert world.halts == []
    assert world.after_run, "the agent called after the run ended and was refused in words"
    assert world.suspended <= world.waits_accepted
    assert not [row for row in state.sessions.invocations if row.phase == SessionPhase.EXECUTING], (
        "no turn is left executing"
    )
    return world


def check_owed(state: CoreState, world: AgentToolWorld) -> None:
    """What every run owes the agent, whatever restarts it went through.

    The budget refuses a second submission of the same candidate only after the agent has read
    the first one's handle; every handle read is one core charged, and one candidate has one
    handle; each charged submission the agent never read is in the stall report.
    """
    charged = {
        handle_for(call.request_id)
        for call in state.evaluation.agent_calls
        if call.request_id is not None
    }
    read_contents = {content for content, _ in world.read}
    assert set(world.budget_refused) <= read_contents, "refused a retry whose handle was never read"
    assert {handle for _, handle in world.read} <= charged
    for content in read_contents:
        assert len({handle for seen, handle in world.read if seen == content}) == 1
    unread = charged - {handle for _, handle in world.read}
    assert all(any(handle in note for handle in charged) for note in world.unacknowledged)
    assert all(any(handle in note for note in world.unacknowledged) for handle in unread)


_STEPS = st.one_of(
    st.sampled_from((("submit",), ("lose",), ("timeout_retry",))),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple)),
    st.tuples(st.just("forged"), st.sampled_from(("foreign", "garbage"))),
    st.tuples(st.just("raw"), st.binary(max_size=80)),
)
_PROGRAM = st.lists(_STEPS, max_size=3).map(tuple)
_TURNS = st.lists(
    st.builds(Turn, program=_PROGRAM, late=_PROGRAM, edit=st.booleans()), min_size=1, max_size=3
).map(tuple)

_SETTINGS = settings(
    max_examples=examples(pr=5, nightly=60),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _play(
    tmp_path: Path, turns: tuple[Turn, ...], total: int, *, finished: bool = False
) -> SimulatedRun:
    """A run whose agent plays ``turns``, in a directory of its own (an example gets a new one)."""
    root = tmp_path / f"run-{next(_RUNS)}"
    root.mkdir()
    return simulate(
        AgentToolWorld(root, turns, total=total, finished_in_submit_round=finished), STEP_LOOP
    )


def test_an_agent_submits_through_the_tool_and_core_measures_the_candidate_once(
    tmp_path: Path,
) -> None:
    run = _play(tmp_path, (Turn(program=(("submit",),)),), 1)
    world = check(run)

    state = run.core
    assert world.errors == []
    assert len(world.handles) == 1
    admitted = [c for c in state.evaluation.agent_calls if c.request_id is not None]
    assert len(admitted) == 1
    # The baseline, the agent's submission, and the strategy's own candidate measurement.
    purposes = sorted(
        row.request.plan.purpose
        for row in state.records.records
        if isinstance(row.request, SubmitMeasurement)
    )
    assert purposes == ["baseline", "local-validation", "official"]
    assert world.unacknowledged == (), "the client confirmed it read the handle"


@pytest.mark.parametrize("kind", [StopKind.OPERATOR_CANCEL, StopKind.OPERATOR_DRAIN])
def test_a_run_stopped_while_an_agent_evaluates_ends_and_the_agent_is_refused_in_words(
    tmp_path: Path, kind: StopKind
) -> None:
    """A stop ends the run wherever it lands in the agent's evaluation; the agent's later calls are typed refusals."""
    normal = _play(tmp_path, (Turn(program=SUBMIT_AND_WAIT),), 1)
    for point in normal.stop_points():
        stopped = normal.stop(point, kind)
        why = f"{kind.value} at {point}: {stopped.why_not_ended}"
        assert stopped.ended, why
        assert stopped.failure is None, why


@_SETTINGS
@given(turns=_TURNS, total=st.integers(1, 2), finished=st.booleans())
def test_no_tool_call_sequence_halts_the_run(
    tmp_path: Path, turns: tuple[Turn, ...], total: int, *, finished: bool
) -> None:
    """Whatever the agents call, before, between and after their turns, the run goes on.

    A submission's job may still be running or already finished when the executor accepts it.
    """
    check(_play(tmp_path, turns, total, finished=finished))


SUBMIT_AND_WAIT: Program = (("submit",), ("wait", (0,)))


def test_a_submit_from_a_workspace_holding_a_nested_repository_is_refused_not_halted(
    tmp_path: Path,
) -> None:
    """Host Git refuses the snapshot; the agent is told how to fix it and the run goes on."""
    world = check(_play(tmp_path, (Turn(nested=True),), 1))

    assert len(world.errors) == 1
    assert "nested Git repository" in world.errors[0]


def test_a_turn_whose_wait_is_refused_at_its_end_releases_the_handles_it_waited_on(
    tmp_path: Path,
) -> None:
    """The gate fails when the turn ends: no yield, and the turn's handles are forgotten."""
    run = _play(tmp_path, (Turn(program=SUBMIT_AND_WAIT, closing_at_yield=True),), 1)
    world = check(run)

    assert world.waits_accepted == 1
    assert run.core.evaluation.continuations == ()


@pytest.mark.parametrize(
    "turns",
    [
        (Turn(late=(("submit",), ("wait", (0,)))),),
        (Turn(late=(("submit",),)), Turn(program=SUBMIT_AND_WAIT)),
        (Turn(program=SUBMIT_AND_WAIT),),
    ],
    ids=["late-submit-then-wait", "late-submit-then-next-turn-waits", "submit-then-wait"],
)
@pytest.mark.parametrize("total", [1, 2])
@pytest.mark.parametrize("finished", [False, True], ids=["running", "finished-at-submission"])
def test_a_submission_running_or_finished_at_a_resume_does_not_halt_the_run(
    tmp_path: Path, turns: tuple[Turn, ...], total: int, *, finished: bool
) -> None:
    """A resume is never authorized over an attempt history a running submission leaves open.

    A submission the executor answered as finished wakes the turns that wait on it.
    """
    check(_play(tmp_path, turns, total, finished=finished))


_LOSSY_STEP = st.sampled_from((("submit",), ("lose",), ("timeout_retry",)))


@_SETTINGS
@given(program=st.lists(_LOSSY_STEP, min_size=1, max_size=4).map(tuple))
def test_a_repeated_submit_is_answered_with_the_one_evaluation_whatever_the_latency(
    tmp_path: Path, program: Program
) -> None:
    """However many times the agent asks, lost replies and retries included, it gets one handle.

    The candidate is charged once, by the first call that reaches the host. Every later call for
    it (a retry of a lost reply, or a repeat after the agent read the handle) is answered with
    that submission and refused never. A submission whose last reply was lost is listed as owed,
    so a stalled run can name it.
    """
    run = _play(tmp_path, (Turn(program=program),), 1)
    world = check(run)

    admitted = {c.request_id for c in run.core.evaluation.agent_calls if c.request_id is not None}
    assert len(admitted) == 1
    (request,) = admitted
    assert world.errors == []
    assert set(world.handles) <= {handle_for(request)}
    assert len(world.handles) == sum(kind != "lose" for kind, *_ in program)
    notes = world.unacknowledged
    assert len(notes) == (1 if program[-1][0] == "lose" else 0)
    assert all(handle_for(request) in note for note in notes)


def test_a_submit_slower_than_the_agents_deadline_is_one_submission_the_agent_can_wait_on(
    tmp_path: Path,
) -> None:
    """The failing live run: the first call outlasts the deadline, the retry must get its handle.

    Before the fix the retry was refused as an exhausted budget while the lost call's
    measurement ran unseen, and the run ended stalled with ``open intents []``.
    """
    world = check(_play(tmp_path, (Turn(program=(("timeout_retry",), ("wait", (0,)))),), 1))

    assert world.errors == []
    assert len(world.handles) == 1
    assert world.waits_accepted == 1, "the agent could wait on the submission it retried"


_RESTART_STEPS = st.one_of(
    st.sampled_from((("submit",), ("lose",), ("timeout_retry",))),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 2), min_size=1, max_size=2).map(tuple)),
)
_RESTARTED = st.lists(
    st.builds(Turn, program=st.lists(_RESTART_STEPS, max_size=4).map(tuple), edit=st.booleans()),
    min_size=1,
    max_size=3,
).map(tuple)


INTERRUPTIONS = (
    (Action.KILL, Moment.IN_FLIGHT),
    (Action.PAUSE, Moment.IN_FLIGHT),
    (Action.PAUSE, Moment.BEFORE_COMMIT),
)
"""Where the host dies or the operator pauses, besides the commit boundaries: the agent's calls cut
in the middle of its turn, and the turn's end held back by a pause."""


def _some[T](points: tuple[T, ...], sample: int) -> list[T]:
    """A sample of ``points`` at pull-request depth, all of them under ``--depth=nightly``."""
    count = len(points) if depth() is Depth.NIGHTLY else sample
    return [points[index] for index in evenly_spaced(len(points), count)]


def _swept(run: SimulatedRun) -> None:
    """Kill the host at a sample of commit boundaries, and interrupt it where its agent is calling."""
    for crash in _some(run.crash_points(), SAMPLE_CRASHES):
        recovered = run.rerun_with_crash(crash)
        check_owed(recovered.core, _world(recovered.world))
        assert _world(recovered.world).halts == [], crash
    for action, moment in INTERRUPTIONS:
        for point in _some(run.interruptions(action, moment), SAMPLE_INTERRUPTIONS):
            interrupted = run.interrupt(point)
            why = f"{point}: {interrupted.outcome.value}; {interrupted.detail}"
            assert interrupted.never_stuck(), why
            assert interrupted.core is not None, why
            check_owed(interrupted.core, _world(interrupted.world))
            assert _world(interrupted.world).halts == [], why


@_SETTINGS
@example(turns=(Turn(program=(("submit",),), edit=False),))
@example(turns=(Turn(program=(("lose",),)), Turn(program=(("submit",),), edit=False)))
@given(turns=_RESTARTED)
def test_no_restart_loses_or_repeats_a_charged_submission(
    tmp_path: Path, turns: tuple[Turn, ...]
) -> None:
    """Hosts die at every commit and effect and the operator pauses; the promise to the agent holds.

    The agent's calls are lost, retried, repeated across turns that may leave the candidate
    unchanged, and cut by the host's death or the pause. After each, a new host starts over the
    same run record and the same owed record.
    """
    run = _play(tmp_path, turns, len(turns))
    check(run)
    _swept(run)


async def test_the_tool_server_is_offered_without_an_interpreter_or_import_path(
    tmp_path: Path,
) -> None:
    """The agent's tool server starts with the one command its image has, in an environment of its own."""
    (descriptor,) = new_bridge(AgentToolWorld(tmp_path, (), total=1)).servers(ROLE, SCOPE)

    assert descriptor.command == "python"
    assert "PYTHONPATH" not in dict(descriptor.env)
