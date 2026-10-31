"""A scope that closes over a job held pending forever closes promptly, and a crash changes nothing.

The world holds every measurement forever, as a scheduler's queue holds a job that nothing ever
starts. The search leaves the workspace that asked for it while the measurement is still pending,
so the scope's close is open beside a step that would only end at its limit, the queue budget past
the wait (twelve hours of run time). The step's kind says nothing outside the scope reads its
result, so the closing scope withdraws it: the loop cancels it and the run goes on at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    Flow,
    ItemId,
    MeasurementPlan,
    MeasurementStage,
    NotOpened,
    SubmitMeasurement,
    Value,
    Within,
    Workspace,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.flows import search
from vs_dst.api import RunSetup, StepLoopHosting, simulate
from vs_runtime.api.testing import Launch
from vs_sim.api.testing import examples

from ._tiny import BASELINE, TinyWorld

if TYPE_CHECKING:
    from vs_core.api import Request
    from vs_proto.api import Record, StepFinished, StepStarted

STEP_LOOP = StepLoopHosting()


class Held(Value):
    """How long the search keeps its workspace open after it asked for the measurement."""

    leaves_after: float


@search("measure-then-leave", config=Held)
async def measure_then_leave(ctx: Flow, config: Held) -> None:
    """Ask for a measurement in a workspace, and leave the workspace without waiting for it."""
    async with (
        ctx.group(stops=False) as asking,
        ctx.workspaces.open(
            attempt=AttemptId(root="attempt:0"),
            item=ItemId(root="item:0"),
            plan=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
            budget=AttemptBudget(),
            within=Within(limit=1000.0),
        ) as workspace,
    ):
        if isinstance(workspace, NotOpened):
            return
        asking.start("measure", measure, workspace)
        await ctx.first(ctx.timer(ctx.clock.now() + config.leaves_after))


async def measure(ctx: Flow, workspace: Workspace) -> None:
    plan = MeasurementPlan(
        purpose="official",
        candidate=BASELINE,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(MeasurementStage(stage_id="measure", execution_budget=100_000.0),),
        policy="ordered",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=ctx.clock.now(),
        queue_allowance=0.0,
        accuracy_gated=(),
    )
    await ctx.evaluation.measure(plan, workspace=workspace, within=5.0)


class HeldWorld(TinyWorld):
    """The tiny world, whose measurements stay pending until the loop cancels them."""

    def __init__(self, leaves_after: float) -> None:
        super().__init__(1)
        self.setup = RunSetup(
            new_launch=lambda: Launch(measure_then_leave, Held(leaves_after=leaves_after)),
            harness=self.setup.harness,
            schemas={},
        )

    def holds(self, request: Request) -> bool:
        return isinstance(request, SubmitMeasurement)


def bodies(records: tuple[Record, ...], field: str) -> list[StepStarted | StepFinished]:
    return [r.body.value for r in records if r.body is not None and r.body.field == field]


def measurement_of(records: tuple[Record, ...]) -> tuple[Record, StepFinished]:
    """The measurement step: the record of its first start, and how it ended (once)."""
    starts = [
        r
        for r in records
        if r.body is not None
        and r.body.field == "step_started"
        and r.body.value.tag.startswith("measurement:")
    ]
    assert starts[0].body is not None
    (finished,) = [s for s in bodies(records, "step_finished") if s.tag == starts[0].body.value.tag]
    return starts[0], finished


def ended_at(records: tuple[Record, ...]) -> float:
    clocks = [r.clock for r in records if r.clock is not None]
    return clocks[-1]


def withdrawn_long_before_its_limit(records: tuple[Record, ...]) -> float:
    """Assert the measurement was withdrawn and the run ended: how long after its start it took."""
    first, finished = measurement_of(records)
    assert first.body is not None
    assert first.clock is not None
    assert finished.result.field == "cancelled"
    assert finished.result.value.name == "WITHDRAWN"
    took = ended_at(records) - first.clock
    assert took < (first.body.value.deadline_at - first.clock) / 2
    return took


@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
@given(leaves_after=st.floats(min_value=0.5, max_value=2000.0))
@example(leaves_after=2.0)
def test_a_scope_closes_over_a_job_held_pending_forever_without_waiting_for_its_limit(
    leaves_after: float,
) -> None:
    run = simulate(HeldWorld(leaves_after), STEP_LOOP)

    assert run.finished
    assert run.halted is None
    assert run.liveness_violations == ()
    assert withdrawn_long_before_its_limit(run.log_records) <= leaves_after + 1.0


def test_a_kill_before_and_after_every_record_still_closes_promptly() -> None:
    """A restarted host runs the held step again and withdraws it again: no wait, same end."""
    normal = simulate(HeldWorld(2.0), STEP_LOOP)
    assert normal.finished

    for crash in normal.crash_points():
        recovered = normal.rerun_with_crash(crash)
        assert recovered.finished, crash
        assert recovered.result == normal.result, crash
        withdrawn_long_before_its_limit(recovered.log_records)
