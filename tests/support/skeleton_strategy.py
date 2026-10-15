"""A scripted minimal search for the composition skeleton, written as a flow.

It plays one scenario: measure the baseline, open one attempt, ask the implementer for
one change, measure the revision that turn produced, keep it as the attempt's candidate,
and let the framework adopt the winner and end the run. It holds no lifecycle or accounting
state of its own; the flow is the whole script.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

import vs_core.api as core
from vs_core.api.search import (
    INPUT,
    Assessment,
    Charge,
    Failed,
    Flow,
    NotOpened,
    NoWinner,
    Role,
    TurnFinished,
    search,
)

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

ATTEMPT = core.AttemptRef(attempt_id=core.AttemptId(root="attempt-0"), generation=0)
DIGEST = "ab" * 32


class Implementation(BaseModel):
    """The implementer's structured reply: the commit it made in its workspace."""

    model_config = ConfigDict(extra="forbid")

    commit: str
    waiting: bool = False


IMPLEMENTER = core.IdScheme("skeleton").role_id("implementer")


class Prompt(core.Value):
    """What the implementer is asked: the context of the skeleton's one prompt."""


class Render(core.RenderRoleArtifactsBase[Prompt]):
    """Render one role prompt."""

    kind: Literal["skeleton.render_role_artifacts"] = "skeleton.render_role_artifacts"


class Verify(core.VerifyParentRevisionBase):
    """Verify a parent revision."""

    kind: Literal["skeleton.verify_parent_revision"] = "skeleton.verify_parent_revision"


class Readings(core.EvidenceReadingsBase[core.Value]):
    """No readings: the skeleton never interprets evidence."""


class Interpret(core.InterpretEvidenceBase):
    """Decode evidence."""

    kind: Literal["skeleton.interpret_evidence"] = "skeleton.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


OPERATIONS = core.StrategyOperations("skeleton", Render, Verify, Interpret)


IMPLEMENTER_ROLE = Role(
    name="implementer",
    prompt="skeleton.j2",
    reply=Implementation,
    access=core.Access.WRITE_CANDIDATE,
    workspace=core.WorkspaceMode.ISOLATED_CHILD,
    charge=Charge.ATTEMPT,
    turn_time=400.0,
    corrections=0,
    lost_turns=0,
    may_wait_for_evaluations=True,
    exchanges=core.Exchanges(resume="skeleton-resume.j2"),
)
IMPLEMENTATION = IMPLEMENTER_ROLE.reply_schema

_RUN_DEADLINE = 1000.0
"""When the skeleton world's run ends (the run clock, in seconds)."""
_MEASURE_SECONDS = 880.0


class SkeletonConfig(core.Value):
    """Which scenario the skeleton plays."""

    measured: bool = True
    keeps_candidate: bool = True
    cancels_after_start: bool = False
    probe: bool = False
    fails: bool = False
    """End at once with no winner: a run that only has to start and finish."""
    idle: bool = False
    """Ask for nothing and wait for the run to be stopped: a run that is open and has no work."""
    wake_at: float | None = None
    """Ask for nothing until this run-clock time, then end with no winner."""
    baseline_only: bool = False
    """Measure the baseline, then end with no winner: no attempt is run."""
    abandons: bool = False
    """Open the one attempt, then end with no winner: the attempt budget is spent, nothing kept."""
    fails_after_choice: bool = False
    """Adopt the retained choice but report failure, preserving the best partial result."""
    turns: int = 1
    """How many implementer turns the attempt asks in one session, one after another."""
    peers: int = 0
    """How many further implementer turns it then asks at once, each in a session of its own."""


def measurement(
    candidate: core.RevisionRef,
    purpose: Literal["baseline", "official"],
    *,
    now_at: float = 0.0,
    facts: core.RunFacts | None = None,
) -> core.MeasurementPlan:
    """An accuracy-then-benchmark plan of one exact revision, ``now_at`` seconds into the clock."""
    evaluator_digest = DIGEST if facts is None else facts.evaluator_digest
    workload_digest = DIGEST if facts is None else facts.workload_digest
    environment_digest = DIGEST if facts is None else facts.environment_digest
    return core.MeasurementPlan(
        purpose=purpose,
        candidate=candidate,
        evaluator_digest=evaluator_digest,
        workload_digest=workload_digest,
        environment_digest=environment_digest,
        stages=tuple(
            core.MeasurementStage(stage_id=name, execution_budget=10.0)
            for name in ("accuracy", "benchmark")
        ),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest=DIGEST),
        submitted_at=now_at,
        queue_allowance=880.0,
        # Absolute run-clock time, past any restart the scenarios make (the run ends at 1000 s).
        accuracy_stage="accuracy",
        accuracy_gated=(core.EvidenceKind.CORRECTNESS, core.EvidenceKind.BENCHMARK),
    )


def _origin(ctx: Flow, config: SkeletonConfig) -> float:
    """The clock time the plans' deadlines count from.

    Zero, so a plan is a pure function of the run's state, except for the probe, which is
    driven on a real clock far from zero.
    """
    return ctx.clock.now() if config.probe else 0.0


async def measure(
    ctx: Flow,
    config: SkeletonConfig,
    revision: core.RevisionRef,
    purpose: Literal["baseline", "official"],
    workspace: core.Workspace | None = None,
) -> NoWinner | None:
    """Measure ``revision``; a failed measurement is the reason the run has no winner."""
    plan = measurement(
        revision,
        purpose,
        now_at=_origin(ctx, config),
        facts=ctx.facts,
    )
    match await ctx.evaluation.measure(plan, workspace=workspace, within=_MEASURE_SECONDS):
        case Failed() as failed:
            return NoWinner(reason=f"measurement {failed.reason}")
        case core.MeasurementResult(failure=failure) if failure is not None:
            return NoWinner(reason=f"measurement {failure.value}")
        case core.MeasurementResult(status=status) if (
            status is not core.ObservationStatus.SUCCEEDED
        ):
            # A rejected or cancelled result may carry no typed failure; it is still no evidence.
            return NoWinner(reason=f"measurement {status.value}")
    return None


async def _probe(ctx: Flow, config: SkeletonConfig) -> None:
    """Submit the input-baseline measurement, then wait for the run to be ended."""
    async with ctx.group(limit=1) as group:
        group.start("baseline", measure, config, ctx.facts.baseline, "baseline")
        await ctx.first(ctx.stopped)


async def _peer_turn(ctx: Flow, workspace: core.Workspace, revision: core.RevisionRef) -> str:
    """One turn in a session of its own; the commit it made, or empty."""
    session = ctx.agents.session(IMPLEMENTER_ROLE, workspace=workspace, revision=revision)
    match await session.turn(Prompt()):
        case TurnFinished(reply=Implementation(commit=commit)):
            return commit
    return ""


async def _turns(ctx: Flow, config: SkeletonConfig, workspace: core.Workspace) -> str:
    """Ask the implementer ``config.turns`` turns in a row, then ``config.peers`` at once.

    The commit of the last turn that finished, or empty when a turn produced none.
    """
    baseline = ctx.facts.baseline
    session = ctx.agents.session(IMPLEMENTER_ROLE, workspace=workspace, revision=baseline)
    commit = ""
    for _ in range(config.turns):
        match await session.turn(Prompt()):
            case TurnFinished(reply=Implementation(commit=made)) if made:
                commit = made
            case _:
                return ""
    if config.peers:
        async with ctx.group() as group:
            for number in range(config.peers):
                group.start(f"peer-{number}", _peer_turn, workspace, baseline)
            async for made in group:
                if isinstance(made, str) and made:
                    commit = made
    return commit


async def _attempt(ctx: Flow, config: SkeletonConfig) -> NoWinner | None:
    """One attempt: implementer turns, the measurement of the last revision, the candidate it keeps."""
    baseline = ctx.facts.baseline
    async with ctx.workspaces.open(
        attempt=ATTEMPT.attempt_id,
        item=core.ItemId(root="item-0"),
        plan=core.WorkspacePlan(mode=core.WorkspaceMode.ISOLATED_CHILD, base=baseline),
        # One paid turn for each turn asked; a resume after a wait is not a paid turn.
        budget=core.AttemptBudget(paid_invocation_limit=config.turns + config.peers),
    ) as workspace:
        if isinstance(workspace, NotOpened):
            return NoWinner(reason="the attempt workspace did not open")
        if config.abandons:
            return NoWinner(reason="the attempt was abandoned")
        if config.cancels_after_start:
            # Wait for the stop; the timer is what keeps the shell from calling the wait a stall.
            await ctx.first(ctx.stopped, ctx.timer(_RUN_DEADLINE))
            return None
        commit = await _turns(ctx, config, workspace)
        if not commit:
            return NoWinner(reason="the implementer turn produced no commit")
        candidate = core.RevisionRef.of_git_commit(commit)
        if config.measured and (
            refused := await measure(ctx, config, candidate, "official", workspace)
        ):
            return refused
        if config.keeps_candidate:
            # Eligible only with accepted evidence for the candidate, which core checks.
            ctx.workspaces.keep(
                workspace, candidate, Assessment(outcome="succeeded", eligible=True)
            )
    return None


@dataclass(frozen=True)
class _Ended:
    """A scenario that ran no attempt, and how its flow ends."""

    result: NoWinner | None


async def _bare(ctx: Flow, config: SkeletonConfig) -> _Ended | None:
    """The scenarios that run no attempt: wait for a stop or a time, or measure the baseline only."""
    if config.idle:
        await ctx.first(ctx.stopped)
        return _Ended(NoWinner(reason="no work was asked for"))
    if config.wake_at is not None:
        await ctx.first(ctx.timer(config.wake_at))
        return _Ended(NoWinner(reason="woken"))
    if config.baseline_only:
        refused = await measure(ctx, config, ctx.facts.baseline, "baseline")
        return _Ended(refused or NoWinner(reason="the baseline is all there is"))
    return None


@search(
    "skeleton",
    config=SkeletonConfig,
    operations=OPERATIONS,
    roles=(IMPLEMENTER_ROLE,),
)
async def skeleton(ctx: Flow, config: SkeletonConfig) -> NoWinner | None:
    """Play the scenario ``config`` names; the framework adopts the chosen winner."""
    if config.fails:
        return NoWinner(reason="scripted")
    if (ended := await _bare(ctx, config)) is not None:
        return ended.result
    if config.probe:
        if config.measured:
            await _probe(ctx, config)
        return None
    if config.measured and (refused := await measure(ctx, config, ctx.facts.baseline, "baseline")):
        return refused
    if (refused := await _attempt(ctx, config)) is not None:
        return refused
    # A retained candidate wins only when core made its settlement eligible, which it does only
    # with accepted evidence for that candidate. Without a measurement the input is all there is.
    eligible = [kept for kept in ctx.workspaces.kept() if kept.settlement.eligible]
    ctx.choose(eligible[0] if eligible else INPUT)
    return NoWinner(reason="out of rounds", keep_choice=True) if config.fails_after_choice else None


DECLARATION = skeleton.declaration


class SkeletonStrategy(core.Value):
    """The scripted scenario; ``measured=False`` omits both measurements.

    The unmeasured variant skips the baseline and candidate measurements, so the
    workspace, session, retirement and adoption interfaces can be driven without the
    measurement ones. Its settlement keeps the candidate but is ineligible (no evidence),
    so the run adopts the trusted baseline.
    """

    config: SkeletonConfig = SkeletonConfig()

    @property
    def declaration(self) -> core.SearchDeclaration:
        """What the host must offer this scenario."""
        return skeleton.declaration

    @property
    def measured(self) -> bool:
        """Whether the scenario measures the baseline and the candidate."""
        return self.config.measured

    @classmethod
    def unmeasured(cls, *, keeps_candidate: bool = True) -> SkeletonStrategy:
        """The scenario without the baseline and candidate measurements.

        With ``keeps_candidate=False`` the attempt is discarded and the trusted
        baseline is adopted, which needs no retained revision.
        """
        return cls(config=SkeletonConfig(measured=False, keeps_candidate=keeps_candidate))

    @classmethod
    def cancelled(cls) -> SkeletonStrategy:
        """Open one attempt and wait there until the run is stopped (the deadline, here).

        No session or measurement is involved: the attempt's workspace is acquired and
        then discarded by the stop, and the run ends with no adopted revision.
        """
        return cls(config=SkeletonConfig(measured=False, cancels_after_start=True))

    @classmethod
    def failing(cls) -> SkeletonStrategy:
        """End at once with no winner, touching no agent, workspace or measurement."""
        return cls(config=SkeletonConfig(fails=True))

    @classmethod
    def baseline_probe(cls) -> SkeletonStrategy:
        """Submit the input-baseline measurement once, then wait for the run to be ended.

        It is a run that is always waiting on a job: for tests of what ends such a run (a
        signal, an operator stop) and what the host then cancels.
        """
        return cls(config=SkeletonConfig(probe=True))

    @classmethod
    def abandoning(cls) -> SkeletonStrategy:
        """Open the one attempt the budget allows, then end with no winner and nothing kept."""
        return cls(config=SkeletonConfig(measured=False, abandons=True))

    @classmethod
    def failing_after_choice(cls) -> SkeletonStrategy:
        """Keep and adopt a verified candidate, then return the strategy's failed outcome."""
        return cls(config=SkeletonConfig(fails_after_choice=True))

    @classmethod
    def idle(cls) -> SkeletonStrategy:
        """Ask for nothing: the run stays open until a control or the deadline ends it."""
        return cls(config=SkeletonConfig(measured=False, idle=True))

    @classmethod
    def baseline_only(cls) -> SkeletonStrategy:
        """Measure the baseline, then end the run with no winner: no attempt is run."""
        return cls(config=SkeletonConfig(baseline_only=True))

    @classmethod
    def waiting_until(cls, at: float) -> SkeletonStrategy:
        """Ask for nothing until the run clock reads ``at``, then end the run."""
        return cls(config=SkeletonConfig(measured=False, wake_at=at))

    def driver(self, registry: OperationRegistry | None = None) -> core.SearchDriver:
        """The driver that plays this scenario."""
        return skeleton.driver(self.config, registry)
