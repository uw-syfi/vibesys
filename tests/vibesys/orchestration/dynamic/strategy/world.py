"""The dynamic strategy's world: what its agents reply and what its benchmarks measure.

`DynamicWorld` is a vs-dst `World`. A test builds one from plain data, simulates it and asserts
on the run::

    world = DynamicWorld(throughputs=(120.0,))
    run = simulate(world)

Keyword arguments that name no world option configure the strategy (`DynamicConfig`), so a
misspelling is refused by name. An executor reports what it saw (a job accepted, a stage
passed, a reply) and never core's conclusions; evidence identities derive from the job's plan,
so a reading and the evidence it decodes always agree.

A test with a fault of its own subclasses the world and answers that request before the base
class does.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, LIMITS, config
from tests.vibesys.orchestration.dynamic.strategy._waiting import (
    WAITING,
    Hosting,
    Measure,
    WaitingTurns,
)

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    DynamicStrategyState,
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    dynamic_operation_registry,
)
from vs_core.api import (
    AdoptRevision,
    ArtifactId,
    ArtifactRef,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EvaluationStageOutcome,
    EvaluationStageResult,
    EvaluationTerminalFacts,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ExecuteRegisteredOperation,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    RunEnvelope,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Failed, Harness, Running, Succeeded, Unknown
from vs_dst.api import RunSetup
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from vs_core.api import (
        CoreState,
        LifecycleCapability,
        Limits,
        MeasurementPlan,
        Request,
        RequestId,
        Scope,
    )
    from vs_dst.api import Enact
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

type Ending = Literal["wait", "result", "invalid"]
ENDINGS: tuple[Ending, ...] = ("wait", "result", "invalid")
"""How a turn that validated its wait can end (see `DynamicWorld`'s ``ends``)."""

BASELINE_THROUGHPUT = 50.0
"""What the input measures; a candidate measures what ``throughputs`` says."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_PLAIN_PROFILE = (True, "profile captured")


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


class DynamicWorld:
    """The planner, implementer, judge, profiler and evaluators of one dynamic run.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994720 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        planner: Sequence[str | Answer] | None = None,
        implementer: Sequence[str | Answer] | None = None,
        judge: Sequence[str | Answer] | None = None,
        profiler: Sequence[str | Answer] = (),
        throughputs: Sequence[float | None] = (100.0,),
        baseline_throughput: float | None = BASELINE_THROUGHPUT,
        profile_reading: tuple[bool, str] = _PLAIN_PROFILE,
        unmeasured: Collection[str] = (),
        retain_per_turn: bool = False,
        unchanged_after_first_retain: bool = False,
        agent_evaluation: bool = False,
        polls: Sequence[int] | None = None,
        waits: Sequence[bool] = (),
        roles: tuple[str, ...] = ("implementer", "judge"),
        submit_only: tuple[str, ...] = (),
        ends: Sequence[Ending] = ("wait",),
        delays: Sequence[int] = (0,),
        checkpoints: Sequence[str] = (),
        limits: Limits = LIMITS,
        deadline_at: float = Harness.deadline_at,
        max_concurrent: int | None = None,
        lifecycle: Collection[LifecycleCapability] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``planner``, ``implementer``, ``judge`` and ``profiler`` are the replies of the n-th
        turn of that role: an agent's JSON, or an answer for a turn that ends another way (a
        `Failed` turn returns nothing usable); a role past its replies is never answered. The
        default is one valid hypothesis, implemented and approved. ``throughputs`` is what
        candidates measure (None: the benchmark fails); a candidate's commit picks its entry.
        ``baseline_throughput`` is what the input measures, ``profile_reading`` what a profile
        stage reads (passed, feedback) and ``unmeasured`` the purposes of the evaluation jobs
        that end without any evidence, as an evaluation whose infrastructure failed does.

        ``retain_per_turn`` retains a new commit after every turn, as a worktree that changed
        between turns would (else one per attempt); ``unchanged_after_first_retain`` returns the
        attempt's first commit for the rest, as production snapshotting does when a resumed turn
        only reads results. ``agent_evaluation`` makes each implementer turn measure its
        workspace through the evaluation tool, as the production bridge does. ``polls`` is how
        many polls the n-th evaluation job reports running (the last entry repeats); given, every
        evaluation is a running job, and without it an agent's own ends as soon as it is accepted.

        ``waits`` makes the n-th fresh turn of ``roles`` (cycling) submit an evaluation through
        the production bridge and wait for it; ``ends`` is how the n-th waiting turn ends once
        it validated its wait (with the waiting reply, the role's final result, or a reply that
        is not JSON); ``submit_only`` names the roles whose fresh turns submit an evaluation and
        end plainly, leaving it running; ``delays`` is the event-loop yields the n-th dispatched
        turn takes before it ends. ``checkpoints`` answers the n-th checkpoint request
        ``succeeded``, ``failed`` or ``retryable`` (cycling).

        ``limits`` and ``deadline_at`` are the run's; ``max_concurrent`` the loop's concurrency
        (default one, or the run's parallelism and two more where turns use the evaluation
        tool); ``lifecycle`` the capabilities the run offers beyond waiting. Any other option
        is a `DynamicConfig` field.
        """
        selected = config(**settings)
        hosted = bool(waits) or bool(submit_only)
        capabilities: frozenset[LifecycleCapability] = frozenset(lifecycle)
        if hosted:
            suspend: LifecycleCapability = "suspend"
            capabilities |= {suspend}
        self.setup = RunSetup(
            new_strategy=lambda: DynamicStrategy(config=selected),
            harness=Harness(
                registry=dynamic_operation_registry(),
                facts=FACTS,
                limits=limits,
                envelope_type=RunEnvelope[DynamicStrategyState],
                requirements=requirements_for(selected),
                deadline_at=deadline_at,
                lifecycle=capabilities,
            ),
            schemas=reply_schemas(selected),
            # A turn that waits is resumed while other requests run, and the evaluation it waits
            # for is admitted to the shell by the loop's control poll.
            max_concurrent=(
                max_concurrent
                if max_concurrent is not None
                else (selected.max_in_flight + 2 if hosted else 1)
            ),
            controlled=hosted,
        )
        self._queues: dict[str, list[str | Answer]] = {
            "orchestrator": list([plan_reply(implement("h1"))] if planner is None else planner),
            "implementer": list([implemented()] if implementer is None else implementer),
            "judge": list([reviewed()] if judge is None else judge),
            "profiler": list(profiler),
        }
        self._throughputs = tuple(throughputs)
        self._baseline = baseline_throughput
        self._profile_reading = profile_reading
        self._unmeasured = frozenset(unmeasured)
        self._retain_per_turn = retain_per_turn
        self._unchanged = unchanged_after_first_retain
        self._agent_evaluation = agent_evaluation
        self._polls = None if polls is None else tuple(polls)
        self._waits = tuple(waits)
        self._roles = roles
        self._submit_only = submit_only
        self._ends = tuple(ends)
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self.hosting = Hosting(delays=tuple(delays))
        self._hosted = hosted or agent_evaluation
        self._started = 0
        self._submitted = 0
        self._calls = 0
        self._running: dict[str, int] = {}
        self._readings: dict[EvidenceId, EvidenceReading] = {}
        self._retained: Counter[str] = Counter()
        self._revisions: dict[str, RevisionRef] = {}

    @property
    def unused(self) -> dict[str, int]:
        """How many replies each role (``orchestrator``, ``implementer``, ...) was never asked for."""
        return {role: len(queue) for role, queue in self._queues.items()}

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        return self._checkpoints(request, core)

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact | None:
        """Serve the evaluation tool to this host's turns that use it (a host without has none)."""
        if not self._hosted:
            return None
        return WaitingTurns(shell, clock, FACTS, self.hosting)

    # -- requests -----------------------------------------------------------

    def _answer(self, request: Request, core: CoreState) -> Answer:
        answer: Answer
        match request:
            case ObserveOwnedJob():
                answer = self._observe(request, core)
            case ExecuteRegisteredOperation():
                answer = self._operation(request)
            case SubmitMeasurement():
                answer = self._submit(request)
            case DispatchTurn() | ResumeSessionTurn():
                answer = self._turn(request, core)
            case _:
                answer = self._environment(request)
        return answer

    def _environment(self, request: Request) -> Answer:
        """What the workspace, session and adoption executors report: they need no script."""
        answer: Answer = Succeeded()
        match request:
            case EnsureWorkspace():
                answer = Succeeded(
                    resource_id=ResourceId(
                        root=f"workspace:{request.scope.owner.root}:{request.attempt.generation}"
                    ),
                    revision=request.plan.base,
                )
            case EnsureSession():
                answer = Succeeded(resource_id=_lease(request.spec.session_id))
            case CloseSession():
                answer = Succeeded(resource_id=_lease(request.session_id))
            case SnapshotAndRetain():
                answer = self._retain(request)
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def _revision(self, attempt: str) -> RevisionRef:
        """The commit the workspace executor retains next for the attempt."""
        name = f"{attempt}:{self._retained[attempt]}" if self._retain_per_turn else attempt
        return RevisionRef.of_git_commit(hashlib.sha256(name.encode()).hexdigest()[:40])

    def _retain(self, request: SnapshotAndRetain) -> Answer:
        attempt = request.attempt.attempt_id.root
        if self._retained[attempt] and self._unchanged:
            return Succeeded(revision=self._revisions[attempt])
        revision = self._revision(attempt)
        self._retained[attempt] += 1
        self._revisions[attempt] = revision
        return Succeeded(revision=revision)

    # -- agents -------------------------------------------------------------

    def _turn(self, request: DispatchTurn | ResumeSessionTurn, core: CoreState) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        lease = _lease(request.turn.session.session_id)
        if isinstance(request, DispatchTurn):
            waiting = self._waits_for(request, role, lease)
            if waiting is not None:
                return waiting
        queue = self._queues[role]
        if not queue:
            return Unknown()
        reply = queue.pop(0)
        if not isinstance(reply, str):
            return reply
        if role == "implementer" and self._agent_evaluation:
            self._measure(request, core)
        return Succeeded(output_json=reply, resource_id=lease)

    def _waits_for(self, request: DispatchTurn, role: str, lease: ResourceId) -> Answer | None:
        """The answer of a fresh turn that waits for its evaluation; None if it does not."""
        assert request.request_id is not None
        if role in self._submit_only:
            self.hosting.submitting.add(request.request_id.root)
        elif role in self._roles and self._waits:
            ordinal = self._started
            self._started += 1
            if self._waits[ordinal % len(self._waits)]:
                self.hosting.waiting.add(request.request_id.root)
                return Succeeded(output_json=self._ending(role, ordinal), resource_id=lease)
        return None

    def _ending(self, role: str, ordinal: int) -> str:
        """The reply the waiting turn ends with."""
        match self._ends[ordinal % len(self._ends)]:
            case "wait":
                return WAITING
            case "invalid":
                return "not json"
            case "result":
                reply = self._queues[role].pop(0)
                assert isinstance(reply, str)
                return reply

    def _measure(self, request: DispatchTurn | ResumeSessionTurn, core: CoreState) -> None:
        """The implementer will measure the revision core retains for its attempt."""
        assert request.request_id is not None
        self._calls += 1
        self.hosting.measuring[request.request_id.root] = Measure(
            candidate=self._revision(request.scope.owner.root),
            now_at=core.run.now_at,
            call=self._calls,
        )

    # -- evaluator ----------------------------------------------------------

    def _submit(self, request: SubmitMeasurement) -> Answer:
        assert request.request_id is not None
        if request.plan.purpose == "local-validation" and self._polls is None:
            return self._finished_agent_evaluation(request)
        self._submitted += 1
        if self._polls is None:
            return Running(resource_id=ResourceId(root=f"job:{self._submitted}"))
        resource = handle_for(request.request_id)
        self._running[resource] = self._polls[min(self._submitted - 1, len(self._polls) - 1)]
        return Running(resource_id=ResourceId(root=resource))

    def _finished_agent_evaluation(self, request: SubmitMeasurement) -> Answer:
        """An agent's evaluation that ends as soon as it is accepted, with trusted evidence."""
        assert isinstance(request.plan.candidate, RevisionRef)
        assert request.request_id is not None
        commit = request.plan.candidate.revision_id.root
        refs: list[EvidenceRef] = []
        stages: list[EvaluationStageResult] = []
        for stage in request.plan.stages:
            reading = self._reading(
                request.plan, _KINDS[stage.stage_id], stage.stage_id, commit
            ).model_copy(update={"kind": EvidenceKind.LOCAL_VALIDATION})
            self._readings[reading.evidence_id] = reading
            refs.append(self._ref(request.scope, request.request_id, request.plan, reading))
            outcome = (
                EvaluationStageOutcome.PASSED if reading.passed else EvaluationStageOutcome.FAILED
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        self._submitted += 1
        return Succeeded(
            resource_id=ResourceId(root=handle_for(request.request_id)),
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(
                stages=tuple(stages),
                accuracy_passed=all(item.passed for item in self._readings.values()),
            ),
        )

    def _observe(self, request: ObserveOwnedJob, core: CoreState) -> Answer:
        resource = request.resource_id
        if self._running.get(resource.root):
            self._running[resource.root] -= 1
            return Running(resource_id=resource)
        job = next(
            item
            for item in core.evaluation.jobs
            if isinstance(item, OwnedJob) and item.resource_id == resource
        )
        plan = job.plan
        assert isinstance(plan.candidate, RevisionRef)
        if plan.purpose in self._unmeasured:
            return Failed(accepted=True)
        commit = plan.candidate.revision_id.root
        refs: list[EvidenceRef] = []
        stages: list[EvaluationStageResult] = []
        for stage in plan.stages:
            reading = self._reading(plan, _KINDS[stage.stage_id], stage.stage_id, commit)
            refs.append(self._ref(job.scope, job.submission_id, plan, reading))
            self._readings[reading.evidence_id] = reading
            outcome = (
                EvaluationStageOutcome.PASSED if reading.passed else EvaluationStageOutcome.FAILED
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        return Succeeded(
            resource_id=resource,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=True),
        )

    @staticmethod
    def _ref(
        scope: Scope, source: RequestId, plan: MeasurementPlan, reading: EvidenceReading
    ) -> EvidenceRef:
        assert isinstance(plan.candidate, RevisionRef)
        return EvidenceRef(
            evidence_id=reading.evidence_id,
            kind=(
                EvidenceKind.LOCAL_VALIDATION
                if plan.purpose == "local-validation"
                else reading.kind
            ),
            purpose=plan.purpose,
            scope=scope,
            source_request=source,
            candidate=plan.candidate,
            observation_sequence=0,
            evaluator_digest=plan.evaluator_digest,
            workload_digest=plan.workload_digest,
            environment_digest=plan.environment_digest,
            provenance="trusted",
            status=ObservationStatus.SUCCEEDED if reading.passed else ObservationStatus.FAILED,
        )

    def _reading(
        self, plan: MeasurementPlan, kind: EvidenceKind, stage: str, commit: str
    ) -> EvidenceReading:
        identifier = EvidenceId(root=f"{stage}:{plan.purpose}:{commit}")
        if kind is EvidenceKind.CORRECTNESS:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        if kind is EvidenceKind.PROFILING:
            passed, feedback = self._profile_reading
            return EvidenceReading(
                evidence_id=identifier, kind=kind, passed=passed, stage=stage, feedback=feedback
            )
        value = self._baseline if plan.purpose == "baseline" else self.headline(commit)
        if value is None:
            return EvidenceReading(
                evidence_id=identifier,
                kind=kind,
                passed=False,
                stage=stage,
                partial=PartialRow(name="throughput", value=1.0, direction="max", unit="tokens/s"),
                feedback="benchmark failed",
            )
        return EvidenceReading(
            evidence_id=identifier,
            kind=kind,
            passed=True,
            stage=stage,
            metrics=(MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),),
        )

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = dynamic_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self._readings[item.evidence_id] for item in typed.evidence),
                )
            )
        assert isinstance(typed, RenderRoleArtifacts)
        return Succeeded(
            outcome=RenderedArtifacts(
                status="succeeded",
                prompts=(
                    ArtifactRef(
                        artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                        digest="prompt",
                    ),
                ),
            )
        )
