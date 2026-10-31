"""The step loop hosts a run, and a run killed or paused anywhere on it resumes to the same end.

The records of its log are the only truth. These cases hold the strategy's part of that: a kill
before or after every record of a run, an advance record included; the whole state the log keeps
when the run parks; a resume on changed code from the latest state after a clean stop and from the
last checkpoint after a crash; and the growth of the log, which is linear in the advances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    EnsureWorkspace,
    Flow,
    FlowState,
    ItemId,
    OperationRegistry,
    ResourceId,
    RunEnvelope,
    Value,
    Within,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.api.recovery import CHECKPOINT_GROWTH
from vs_core.api.testing import Harness, Succeeded, sample_limits, sample_retries
from vs_core.flows import search
from vs_dst.api import (
    Action,
    Hosting,
    Interruption,
    Moment,
    RunSetup,
    ShellHosting,
    SimulatedRun,
    StepLoopHosting,
    simulate,
)
from vs_runtime.api.testing import Launch
from vs_sim.api.testing import examples

from ._tiny import FACTS, PATIENCE, TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer
    from vs_proto.api import Record

STEP_LOOP = StepLoopHosting()
TERMINAL = AttemptPhase.TERMINAL


def fields_of(records: tuple[Record, ...]) -> list[str]:
    return [record.body.field for record in records if record.body is not None]


def test_a_run_on_the_step_loop_ends_as_it_does_on_the_shell() -> None:
    on_step_loop = simulate(TinyWorld(3), STEP_LOOP)
    on_shell = simulate(TinyWorld(3))

    assert on_step_loop.finished
    assert on_step_loop.halted is None
    assert on_step_loop.result == on_shell.result
    assert len(on_step_loop.executed_effects) == len(on_shell.executed_effects)


def test_the_log_of_a_run_starts_with_its_seed_and_ends_with_the_end_of_the_run() -> None:
    fields = fields_of(simulate(TinyWorld(2), STEP_LOOP).log_records)

    assert fields[:2] == ["epoch_started", "run_started"]
    assert fields.count("run_started") == 1
    assert fields[-1] == "run_ended"
    assert fields.count("run_ended") == 1
    assert "strategy_advanced" in fields


def test_a_kill_before_and_after_every_record_resumes_to_the_uninterrupted_run() -> None:
    """Every record of the log is a crash point: the seed, an advance record, a step's records."""
    normal = simulate(TinyWorld(3), STEP_LOOP)
    kinds = {"run_started", "strategy_advanced", "step_started", "step_finished", "run_ended"}
    assert kinds <= set(fields_of(normal.log_records))

    for crash in normal.crash_points():
        recovered = normal.rerun_with_crash(crash)
        assert recovered.finished, crash
        assert recovered.result == normal.result, crash
        assert recovered.no_effect_ran_twice(), crash


def test_a_kill_between_an_advance_and_the_steps_it_started_loses_neither() -> None:
    """The advance record carries the proposals: killed after it, the steps they made still run."""
    normal = simulate(TinyWorld(3), STEP_LOOP)
    records = normal.log_records
    advances = [i for i, field in enumerate(fields_of(records)) if field == "strategy_advanced"]

    for index in advances:
        for moment in (Moment.BEFORE_COMMIT, Moment.AFTER_COMMIT):
            interrupted = normal.interrupt(Interruption(Action.KILL, moment, index))
            assert interrupted.matches_uninterrupted(), (index, moment, interrupted.detail)
            assert interrupted.every_step_finished_once(), (index, moment)


class Progress(Value):
    """What the checkpointing search can be restarted from: where it got to."""

    kind: str = "progress"
    items: int
    done: int = 0
    patience: float = PATIENCE


@search("tiny-restartable", config=Progress, snapshot=Progress)
async def restartable(ctx: Flow, config: Progress) -> None:
    """The tiny search, checkpointing the item it is at, under the attempt names of one version."""
    await run_items(ctx, config, prefix="v1")


async def run_items(ctx: Flow, config: Progress, *, prefix: str) -> None:
    ctx.checkpoint(config)
    for index in range(config.done, config.items):
        async with ctx.workspaces.open(
            attempt=AttemptId(root=f"{prefix}:{index}"),
            item=ItemId(root=f"item:{index}"),
            plan=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
            budget=AttemptBudget(),
            within=Within(limit=config.patience),
        ):
            pass
        ctx.checkpoint(config.model_copy(update={"done": index + 1}))


@search("tiny-restartable", config=Progress, snapshot=Progress)
async def upgraded(ctx: Flow, config: Progress) -> None:
    """The same search after an edit that names its attempts differently: its journal cannot replay."""
    await run_items(ctx, config, prefix="v2")


def restartable_setup(items: int, *, changed: bool) -> RunSetup:
    config = Progress(items=items)

    def launch() -> Launch[Progress]:
        return Launch(restartable, config)

    def upgrade() -> Launch[Progress]:
        return Launch(upgraded, config)

    return RunSetup(
        new_launch=launch,
        upgrade=upgrade if changed else None,
        harness=Harness(
            registry=OperationRegistry(()),
            facts=FACTS,
            limits=sample_limits(
                max_attempts=8, max_turns=8, max_parallel=1, request_retries=sample_retries(2)
            ),
            envelope_type=RunEnvelope[FlowState],
        ),
        schemas={},
    )


class RestartableWorld(TinyWorld):
    """The tiny world over the checkpointing search."""

    def __init__(self, items: int, *, changed: bool) -> None:
        super().__init__(items)
        self.setup = restartable_setup(items, changed=changed)
        self.items = items

    def __call__(self, request: Request, core: CoreState) -> Answer:
        del core
        if isinstance(request, EnsureWorkspace):
            number = self.workspaces_handed_out
            self.workspaces_handed_out += 1
            return Succeeded(
                resource_id=ResourceId(root=f"workspace:{number}"), revision=request.plan.base
            )
        return Succeeded()


def last_advance_of_epoch(records: tuple[Record, ...], epoch: int) -> Record:
    return [
        r for r in records if r.epoch == epoch and r.body and r.body.field == "strategy_advanced"
    ][-1]


def settled_twice(world: RestartableWorld) -> int:
    """How many workspaces the world handed out beyond one per item: the work a restart redid."""
    return world.workspaces_handed_out - world.items


@pytest.mark.parametrize("changed", [False, True], ids=["same-code", "changed-code"])
def test_a_clean_pause_leaves_the_search_whole_in_the_log_and_the_resume_keeps_what_was_done(
    changed: bool,  # noqa: FBT001  # lint-waiver: LW-731420 [FBT001]; pytest passes a parametrized value by keyword, and a keyword-only marker would only restate that.
    # > A keyword-only marker is not what a parametrized test takes from pytest by position; two tests would repeat the body instead.
) -> None:
    """The whole state is the last record of the search at a park, so a restart on changed code
    starts at the latest state: at most the item that was open at the pause is settled again."""
    normal = simulate(RestartableWorld(4, changed=changed), STEP_LOOP)
    assert normal.finished

    for point in normal.interruptions(Action.PAUSE, Moment.AFTER_COMMIT):
        interrupted = normal.interrupt(point)
        assert interrupted.never_stuck(), (point, interrupted.detail)
        assert interrupted.every_step_finished_once(), point
        if interrupted.history[0] != "parked":
            continue  # the run ended before the pause took hold
        whole = last_advance_of_epoch(interrupted.log_records, 1)
        assert whole.body is not None
        assert whole.body.value.strategy.field == "checkpoint", point
        assert isinstance(interrupted.world, RestartableWorld)
        assert settled_twice(interrupted.world) <= 1, (point, settled_twice(interrupted.world))
        if not changed:
            assert interrupted.matches_uninterrupted(), (point, interrupted.detail)
            assert settled_twice(interrupted.world) == 0, point


def test_a_restart_from_an_older_checkpoint_never_reuses_a_decision_id_the_dead_flows_issued() -> (
    None
):
    """After a crash the flows restart from the last checkpoint in the log, which is older than
    the last step. They must number past every id the dead flows issued: an answer for an old
    decision reaching a new one is a ``ContractError`` that ends the run."""
    normal = simulate(RestartableWorld(8, changed=True), STEP_LOOP)

    for moment in (Moment.BEFORE_COMMIT, Moment.AFTER_COMMIT):
        for point in normal.interruptions(Action.KILL, moment):
            interrupted = normal.interrupt(point)
            assert interrupted.never_stuck(), (point, interrupted.detail)
            assert interrupted.every_step_finished_once(), point
            assert interrupted.result is not None, point
            assert isinstance(interrupted.world, RestartableWorld)
            assert settled_twice(interrupted.world) <= interrupted.world.items, point


@pytest.mark.parametrize("hosting", [ShellHosting(), STEP_LOOP], ids=["shell", "step-loop"])
def test_a_resume_on_changed_code_lets_go_of_work_the_new_code_does_not_ask_for(
    hosting: Hosting,
) -> None:
    """A run killed or paused anywhere and resumed on code that names its attempts differently ends,
    with every attempt closed: the work the dead flows had in flight and the new code never asks for
    is carried out through its attempt's closure, which frees the slot only once that is done."""
    normal = simulate(RestartableWorld(4, changed=True), hosting)

    for action in Action:
        for moment in Moment:
            for point in normal.interruptions(action, moment):
                interrupted = normal.interrupt(point)
                assert interrupted.never_stuck(), (point, interrupted.detail)
                assert interrupted.every_step_finished_once(), point
                assert interrupted.core is not None
                held = [a for a in interrupted.core.attempts.attempts if a.phase is not TERMINAL]
                assert not held, (point, [a.attempt_id.root for a in held])


@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@given(items=st.integers(1, 8))
def test_the_log_grows_linearly_with_the_advances_of_the_search(items: int) -> None:
    """Total bytes of the search's records for ``2n`` items stay near twice those for ``n``.

    Logging the whole state at each advance would square the growth: a whole state is the journal so
    far. Between checkpoints the log keeps the steps, and a checkpoint is due only when the advance
    count has multiplied by ``CHECKPOINT_GROWTH``, so the checkpoints of a run add a bounded
    multiple of its last one.
    """
    small = search_bytes(
        simulate(RestartableWorld(items * 4, changed=False), STEP_LOOP).log_records
    )
    large = search_bytes(
        simulate(RestartableWorld(items * 8, changed=False), STEP_LOOP).log_records
    )

    slack = CHECKPOINT_GROWTH / (CHECKPOINT_GROWTH - 1) + 0.5
    assert large <= 2 * small * slack, (small, large)


def search_bytes(records: tuple[Record, ...]) -> int:
    return sum(
        len(r.body.value.to_binary())
        for r in records
        if r.body is not None and r.body.field == "strategy_advanced"
    )


WORLDS = {
    f"{name}-{label}": (hosting, make)
    for label, hosting in (("shell", ShellHosting()), ("step-loop", STEP_LOOP))
    for name, make in (
        ("tiny", lambda: TinyWorld(3)),
        ("restartable", lambda: RestartableWorld(4, changed=False)),
    )
}


@dataclass(frozen=True)
class Stops:
    """A finished run and the places the operator can stop it, a first time and a second."""

    normal: SimulatedRun
    first: tuple[Interruption, ...]
    second: tuple[Interruption, ...]


@pytest.fixture(scope="module", params=sorted(WORLDS))
def stops(request: pytest.FixtureRequest) -> Stops:
    hosting, make = WORLDS[request.param]
    normal = simulate(make(), hosting)
    pauses = normal.interruptions(Action.PAUSE)
    return Stops(normal, pauses + normal.interruptions(Action.KILL), pauses)


@settings(max_examples=examples(pr=8, nightly=60), deadline=None)
@given(data=st.data())
def test_a_run_resumed_and_stopped_again_ends_as_the_uninterrupted_run(
    stops: Stops, data: st.DataObject
) -> None:
    """The host that resumed a run is itself paused, anywhere: the run ends as it would have, and
    every step finishes once."""
    first = data.draw(st.sampled_from(stops.first))
    second = data.draw(st.sampled_from(stops.second))
    interrupted = stops.normal.interrupt(first, then=second)

    assert interrupted.never_stuck(), (first, second, interrupted.detail)
    assert interrupted.matches_uninterrupted(), (first, second, interrupted.detail)
    assert interrupted.every_step_finished_once(), (first, second)


def test_a_second_pause_takes_hold_of_the_host_that_resumed_the_run(stops: Stops) -> None:
    """The generator above reaches something: some second pause parks the resumed host again.

    The first stop is a pause mid-run, after a commit the host is open for: one before the host
    has opened its log is not delivered, and stops nothing.
    """
    mid_run = [p for p in stops.second if p.moment is Moment.AFTER_COMMIT]
    first = mid_run[len(mid_run) // 2]
    parked = [
        stops.normal.interrupt(first, then=second).history.count("parked")
        for second in stops.second
        if second != first
    ]

    assert max(parked) >= 2, parked


def test_a_resumed_host_is_stopped_again_by_a_pause_and_not_by_a_kill() -> None:
    normal = simulate(TinyWorld(2), STEP_LOOP)
    first, *_ = normal.interruptions(Action.PAUSE)
    kill = normal.interruptions(Action.KILL)[0]

    with pytest.raises(ValueError, match="stopped again by a pause"):
        normal.interrupt(first, then=kill)
