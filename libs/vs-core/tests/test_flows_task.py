"""The task a flow reads, and a workspace kept without a revision.

``ctx.task`` carries what the task fixes (its terms) from the run's facts: a run without terms
cannot be asked for them. A workspace may be kept with no revision (the work made none): the
settlement then asserts only how the workspace ended.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FlowRun, fake_search

from .flow_support import FACTS, ROLES, Config, open_workspace

STAGES = ("accuracy", "benchmark", "profile")


def terms(measured: tuple[str, ...], *, accuracy_gated: bool = False) -> core.TaskTerms:
    """A task over three stages that measures ``measured`` of them."""
    return core.TaskTerms(
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        stages=tuple(core.TaskStage(stage_id=name, execution_seconds=7.0) for name in STAGES),
        measured=measured,
        queue_allowance=3.0,
        accuracy_stage="accuracy" if "accuracy" in measured else None,
        accuracy_gated=(core.EvidenceKind.BENCHMARK,) if accuracy_gated else (),
        operation_seconds=5.0,
    )


def facts_with(task: core.TaskTerms | None) -> core.RunFacts:
    return FACTS.model_copy(update={"task": task})


class Seen(core.Value):
    """What the flow read of its task."""

    measured: tuple[str, ...]
    stage_seconds: tuple[float, ...]
    run_id: str


@fake_search("flows", Config, ROLES)
async def read_task(ctx: core.Flow, _config: Config) -> None:
    task = ctx.task.terms
    recipe = core.MeasurementRecipe.of(ctx.facts)
    plan = recipe.plan(ctx.facts.baseline, purpose="baseline", now_at=0.0, stages=task.measured)
    ctx.publish(
        Seen(
            measured=task.measured,
            stage_seconds=tuple(stage.execution_budget for stage in plan.stages),
            run_id=ctx.task.run_id,
        )
    )


@given(
    measured=st.lists(st.sampled_from(STAGES), unique=True).map(tuple),
    gated=st.booleans(),
)
def test_a_flow_reads_the_tasks_terms_and_builds_its_plans_from_them(
    measured: tuple[str, ...], *, gated: bool
) -> None:
    task = terms(measured, accuracy_gated=gated)
    run = FlowRun.start(read_task, Config(), facts=facts_with(task))
    seen = run.published(Seen)
    assert seen is not None
    assert seen.measured == measured
    assert seen.stage_seconds == (7.0,) * len(measured)
    assert seen.run_id == core.initial_state().run.run_id.root


def test_a_run_without_task_terms_cannot_be_asked_for_them() -> None:
    with pytest.raises(core.ContractError, match="no task terms"):
        FlowRun.start(read_task, Config(), facts=facts_with(None))


def test_terms_that_measure_a_stage_the_task_lacks_are_refused() -> None:
    with pytest.raises(ValueError, match="names stages the task lacks"):
        core.TaskTerms(
            recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="r"), digest="r"),
            stages=(core.TaskStage(stage_id="accuracy", execution_seconds=1.0),),
            measured=("benchmark",),
            queue_allowance=1.0,
            operation_seconds=1.0,
        )


@given(measured=st.lists(st.sampled_from(STAGES), unique=True).map(tuple))
def test_an_accuracy_proof_is_required_exactly_when_the_task_measures_accuracy(
    measured: tuple[str, ...],
) -> None:
    assert terms(measured).requires_accuracy_proof is ("accuracy" in measured)


KEPT = core.Assessment(outcome="failed", retention="discard")


@fake_search("flows", Config, ROLES)
async def keep_nothing(ctx: core.Flow, _config: Config) -> None:
    async with open_workspace(ctx, "ws") as ws:
        assert not isinstance(ws, core.NotOpened)
        ctx.workspaces.keep(ws, None, KEPT)


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
def test_a_workspace_kept_without_a_revision_settles_with_the_assessment_and_no_candidate(
    *, restarting: bool
) -> None:
    run = FlowRun.start(keep_nothing, Config(), restarting=restarting)
    run.open_workspace(run.expect(core.StartAttempt))
    settle = run.expect(core.Withdraw)
    request = settle.request
    assert isinstance(request, core.Withdraw)
    disposition = request.disposition
    assert isinstance(disposition, core.Settle)
    assert disposition.candidate is None
    assert (disposition.outcome, disposition.retention) == ("failed", "discard")
    settlement = run.close_workspace(settle)
    assert settlement.candidate is None


@pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart-every-step"])
@pytest.mark.parametrize("code", list(core.RejectionCode))
@pytest.mark.parametrize("refusals", [1, 2], ids=["assertion-refused", "every-close-refused"])
def test_a_workspace_core_refuses_to_settle_as_asked_settles_plainly_and_says_why(
    *, restarting: bool, code: core.RejectionCode, refusals: int
) -> None:
    """Whatever code core refuses with, and even if it refuses the plain close too, the flow ends."""
    seen: list[core.Failed | None] = []

    @fake_search("flows", Config, ROLES)
    async def refused(ctx: core.Flow, _config: Config) -> None:
        async with open_workspace(ctx, "ws") as ws:
            assert not isinstance(ws, core.NotOpened)
            ctx.workspaces.keep(ws, None, KEPT)
        seen.append(ws.refused)

    run = FlowRun.start(refused, Config(), restarting=restarting)
    run.open_workspace(run.expect(core.StartAttempt))
    run.fail(run.expect(core.Withdraw), core.FailureReason.REFUSED, cause=code)
    plain = run.expect(core.Withdraw)
    assert isinstance(plain.request, core.Withdraw)
    assert isinstance(plain.request.disposition, core.Settle)
    assert plain.request.disposition.outcome == "failed"
    if refusals == 1:
        run.close_workspace(plain)
    else:
        run.fail(plain, core.FailureReason.REFUSED, cause=code)
    assert seen
    failure = seen[-1]
    assert failure is not None
    assert failure.reason is core.FailureReason.REFUSED
    assert failure.cause == code
    assert not [item for item in run.open if isinstance(item.request, core.Withdraw)]
