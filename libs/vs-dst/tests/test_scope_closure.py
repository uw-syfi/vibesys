"""A scope that is closed releases what it holds in order, and its settlement comes last (#3689 D).

The search leaves a workspace that holds an agent turn and a measurement job, in every order
those can end. Whatever the order, and whatever interrupts the run (a kill, a pause or a drain at
any commit or effect, a kill inside the recovery's ``clear``, an operator's pause), the log must
show what closing a scope promises, read from the run.log and the folds of its prefixes:

- the close runs after every other step of its scope has a result (a step that waits for the
  close itself, such as the workspace's discard or a session's close, is the close's own cleanup);
- the scope's settlement is published once, after the close, the cleanup it asked for, and the
  release of every job and session the scope held, and no earlier prefix of the log shows it;
- the slot is released once, with the settlement: never before, never again;
- the run ends once, with a result for every step it started.

The properties name no mechanism: the same tests must stay green when the rules that decide them
change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from protobuf import Oneof

from vs_core.api import CloseAttemptScope, RequestPhase, SessionPhase
from vs_core.api.recovery import fold
from vs_dst.api import (
    Action,
    Interruption,
    Moment,
    OperatorRequest,
    Outcome,
    StepLoopHosting,
    simulate,
)
from vs_runtime.api.infrastructure import PauseRequest
from vs_sim.api.testing import examples

from ._scoped import Held, HeldWorld, every_order, settles_over_an_open_inspection

if TYPE_CHECKING:
    from vs_core.api import AttemptRef, CoreState, OperationRegistry, Request
    from vs_dst.api import Interrupted
    from vs_proto.api import Record

STEP_LOOP = StepLoopHosting()
EPOCH = 1


def _steps(records: tuple[Record, ...]) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """The positions in the log at which each tag started and finished."""
    started: dict[str, list[int]] = {}
    finished: dict[str, list[int]] = {}
    for position, record in enumerate(records):
        match record.body:
            case Oneof(field="step_started", value=message):
                started.setdefault(message.tag, []).append(position)
            case Oneof(field="step_finished", value=message):
                finished.setdefault(message.tag, []).append(position)
    return started, finished


def _follows(asked: dict[str, Request], request: Request, tag: str) -> bool:
    """Whether ``request`` waits, directly or through other steps, for the step ``tag``."""
    pending = [dependency.root for dependency in request.depends_on]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == tag:
            return True
        if current not in seen:
            seen.add(current)
            if current in asked:
                pending.extend(dependency.root for dependency in asked[current].depends_on)
    return False


def _closes(asked: dict[str, Request]) -> list[str]:
    return [tag for tag, request in asked.items() if isinstance(request, CloseAttemptScope)]


def _the_close_follows_the_work(
    records: tuple[Record, ...], asked: dict[str, Request], why: str
) -> None:
    """Every start of a scope's close finds the scope's other work ended, and nothing starts after."""
    started, finished = _steps(records)
    for close in _closes(asked):
        scope = asked[close].scope
        work = [
            tag
            for tag, request in asked.items()
            if tag != close and request.scope == scope and not _follows(asked, request, close)
        ]
        for begun in started.get(close, ()):
            for tag in work:
                opened = [p for p in started.get(tag, ()) if p < begun]
                ended = [p for p in finished.get(tag, ()) if p < begun]
                assert not opened or ended, f"{why}: the close began over open work {tag}"
                later = [p for p in started.get(tag, ()) if p > begun]
                assert not later, f"{why}: {tag} began after the close did"


def _scope_of(core: CoreState, attempt: AttemptRef) -> tuple[list[bool], list[bool]]:
    """Whether each job and each session the attempt's scope holds is released, in ``core``."""
    jobs = [
        job.released
        for job in (*core.evaluation.jobs, *core.evaluation.registered_jobs)
        if job.scope.owner == attempt.attempt_id
    ]
    sessions = [
        row.phase is SessionPhase.TERMINAL
        for row in core.sessions.sessions
        if row.scope.owner == attempt.attempt_id
    ]
    return jobs, sessions


def _settlement_comes_last(
    records: tuple[Record, ...], registry: OperationRegistry, why: str
) -> None:
    """Fold every prefix of the log: the settlement and the slot's release appear once, together."""
    first = next(
        i for i, r in enumerate(records) if r.body is not None and r.body.field == "run_started"
    )
    settled_at: dict[AttemptRef, int] = {}
    released_at: dict[AttemptRef, int] = {}
    held: set[AttemptRef] = set()
    core = fold(records[: first + 1], registry=registry)
    for end in range(first + 1, len(records) + 1):
        if end > first + 1:
            core = fold(records[end - 1 : end], core, registry=registry)
        slots = {slot.attempt for slot in core.scheduling.slots}
        assert not slots & released_at.keys(), f"{why}: a released slot was taken again"
        for attempt in held - slots:
            released_at[attempt] = end
        held = slots
        for row in core.settlement.settlements:
            if row.attempt not in settled_at:
                settled_at[row.attempt] = end
                _ended_what_it_held(core, row.attempt, why)
    assert released_at == settled_at, (
        f"{why}: slots released at {released_at}, settled at {settled_at}"
    )
    assert not held, f"{why}: a slot is still held after the run ended"


def _ended_what_it_held(core: CoreState, attempt: AttemptRef, why: str) -> None:
    """At the settlement's first prefix: every job and session of the scope is released, and no step of it is open."""
    jobs, sessions = _scope_of(core, attempt)
    assert all(jobs), f"{why}: settled with a job of the scope not released"
    assert all(sessions), f"{why}: settled with a session of the scope open"
    still_open = [
        record.request_id.root
        for record in core.records.records
        if record.request.scope.owner == attempt.attempt_id
        and record.phase is not RequestPhase.COMPLETED
    ]
    assert not still_open, f"{why}: settled with {still_open} open"


def check_closure(records: tuple[Record, ...], world: HeldWorld, why: str) -> None:
    """The closure invariants of a run's log."""
    registry = world.setup.harness.registry
    _the_close_follows_the_work(records, world.asked, why)
    _settlement_comes_last(records, registry, why)


def test_a_scope_closed_in_each_order_ends_what_it_held_before_it_settles() -> None:
    for config in every_order():
        run = simulate(HeldWorld(config), STEP_LOOP)
        assert run.finished, config
        check_closure(run.log_records, _world(run.world), str(config))


def _world(world: object) -> HeldWorld:
    assert isinstance(world, HeldWorld)
    return world


def _ends(records: tuple[Record, ...]) -> int:
    return sum(r.body is not None and r.body.field == "run_ended" for r in records)


def _after(
    interrupted: Interrupted, point: Interruption, config: Held, *, drained: bool = False
) -> None:
    """The run recovered from ``point``, and its log still closes the scope as promised."""
    why = f"{interrupted.interruption} after {point}: {interrupted.detail}"
    assert interrupted.never_stuck(), f"progress: {interrupted.outcome.value}, {why}"
    assert interrupted.outcome is not Outcome.ERRORED, why
    assert 1 <= _ends(interrupted.log_records) <= 1 + drained, f"one ending, {why}"
    if not settles_over_an_open_inspection(config):
        check_closure(interrupted.log_records, _world(interrupted.world), why)


def test_a_kill_with_work_of_a_closing_scope_in_flight_never_leaves_the_run_waiting() -> None:
    """The scope's render or measurement, never begun when the search leaves, is still answered (#3712).

    The restarted host finds the scope closing before the interrupted step runs again, so core
    ends it itself, unsent. Its asker hears that, and the run ends.
    """
    for config in every_order():
        normal = simulate(HeldWorld(config), STEP_LOOP)
        for point in normal.interruptions(Action.KILL, Moment.IN_FLIGHT):
            interrupted = normal.interrupt(point)
            assert interrupted.never_stuck(), f"{config} {point}: {interrupted.detail}"


@settings(max_examples=examples(pr=15, nightly=300), deadline=None)
@given(
    config=st.sampled_from(every_order()),
    action=st.sampled_from((Action.KILL, Action.PAUSE, Action.STOP)),
    moment=st.sampled_from((Moment.BEFORE_COMMIT, Moment.AFTER_COMMIT, Moment.IN_FLIGHT)),
    choice=st.integers(min_value=0, max_value=10_000),
)
def test_a_run_interrupted_anywhere_still_closes_its_scope_in_order(
    config: Held, action: Action, moment: Moment, choice: int
) -> None:
    """Kill, pause or drain the host at any commit or in-flight effect: closure keeps its order."""
    normal = simulate(HeldWorld(config), STEP_LOOP)
    points = normal.interruptions(action, moment)
    point = points[choice % len(points)]

    _after(normal.interrupt(point), point, config, drained=action is Action.STOP)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(
    config=st.sampled_from(every_order()),
    second=st.sampled_from((Action.KILL, Action.STOP, Action.PAUSE)),
    choice=st.integers(min_value=0, max_value=10_000),
)
def test_a_second_interruption_inside_the_clear_still_closes_the_scope_in_order(
    config: Held, second: Action, choice: int
) -> None:
    """The host that takes over after a kill is interrupted at an await of its ``clear``."""
    normal = simulate(HeldWorld(config), STEP_LOOP)
    kills = [
        *normal.interruptions(Action.KILL, Moment.IN_FLIGHT),
        *normal.interruptions(Action.KILL, Moment.BEFORE_COMMIT),
    ]
    first = kills[choice % len(kills)]
    awaits = normal.interrupt(first).effects.clear_awaits
    if awaits == 0:
        return
    point = Interruption(second, Moment.IN_CLEAR, choice % awaits)

    interrupted = (
        normal.interrupt_again(first, point)
        if second is Action.KILL
        else normal.interrupt(first, then=point)
    )

    _after(interrupted, first, config, drained=second is Action.STOP)


@settings(max_examples=examples(pr=10, nightly=200), deadline=None)
@given(
    config=st.sampled_from(every_order()),
    choice=st.integers(min_value=0, max_value=10_000),
    kill=st.booleans(),
)
def test_an_operator_pause_at_any_point_still_closes_the_scope_in_order(
    config: Held, choice: int, *, kill: bool
) -> None:
    """The operator's pause through the control inbox, with the host killed after it or not."""
    normal = simulate(HeldWorld(config), STEP_LOOP)
    points = [p for moment in Moment for p in normal.interruptions(Action.PAUSE, moment)]
    point = points[choice % len(points)]
    pause = OperatorRequest(
        point.moment, point.index, PauseRequest(control_id="p", expected_epoch=EPOCH)
    )
    kills = normal.interruptions(Action.KILL)

    interrupted = normal.operate([pause], kills[choice % len(kills)] if kill else None)

    _after(interrupted, point, config)
