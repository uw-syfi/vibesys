"""The single strategy's world: what its agents reply and what its benchmarks measure.

`SingleWorld` is a vs-dst `World`. A test builds one from plain data, simulates it and asserts
on the run::

    world = SingleWorld(throughputs=(120.0,), passes=(True,))
    run = simulate(world)

Keyword arguments that name no world option configure the strategy (`SingleConfig`), so a
misspelling is refused by name. An executor reports what it saw (a job accepted, a stage
passed, a reply) and never core's conclusions; evidence identities derive from the job's plan, so
a reading and the evidence it decodes always agree.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.vibesys.orchestration.single.strategy._waiting import WAITING, WaitingTurns

from vibesys.orchestration.single.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.single.strategy.api import (
    CaptureCommand,
    CommandCapture,
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    RenderRoleArtifacts,
    SingleConfig,
    SingleStrategy,
    SingleStrategyState,
    VerifyParentRevision,
    single_operation_registry,
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
    Limits,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    ResourceId,
    ResumeSessionTurn,
    RetainRevision,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    SessionId,
    SnapshotAndRetain,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.testing.drive import Answer, Harness, Running, Succeeded, Unknown
from vs_dst.api import RunSetup
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreState, MeasurementPlan, Request, RequestId, Scope
    from vs_dst.api import Enact
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

FACTS = RunFacts(
    objective="make it faster",
    baseline=RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)
LIMITS = Limits(max_attempts=4, max_turns=60, max_parallel=1, max_retries=2, max_refunds=4)
BASELINE_THROUGHPUT = 50.0
"""What the input measures; a candidate measures what ``throughputs`` says."""

type Ending = Literal["wait", "result", "invalid"]
ENDINGS: tuple[Ending, ...] = ("wait", "result", "invalid")
"""How a turn that validated its wait can end (see `SingleWorld`'s ``ends``)."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}


def plan_reply(identifier: str = "h1", **extra: object) -> str:
    """A designer reply: one new hypothesis."""
    return json.dumps(
        {
            "hypothesis_id": identifier,
            "title": f"title {identifier}",
            "hypothesis": f"claim {identifier}",
            "task": f"task {identifier}",
            "pass_criteria": "faster",
            "reasoning": "because",
            **extra,
        }
    )


def implemented(*, passed: bool = True, **extra: object) -> str:
    """A combined implement, self-review and profile reply."""
    return json.dumps(
        {
            "summary": "did it",
            "expected_behavior": "faster",
            "self_review": "ok",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            "bottlenecks": "",
            "suggestions": "",
            "profile_analysis": "",
            **extra,
        }
    )


def profile_attribution() -> str:
    """What an attribution command prints (profile result protocol v1)."""
    return json.dumps(
        {
            "version": 1,
            "cost_unit": "ms",
            "components": [
                {"name": "gemm", "cost": 8.0, "share": 0.8, "evidence": ["a"]},
                {"name": "io", "cost": 2.0, "share": 0.2, "evidence": []},
            ],
        }
    )


def single_config(**settings: object) -> SingleConfig:
    """The strategy's config: two rounds unless ``settings`` say otherwise."""
    return SingleConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 2,
            **settings,
        }
    )


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def _revision(attempt: str) -> RevisionRef:
    """The commit the workspace executor retains for an attempt: fixed by the attempt."""
    return RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])


class SingleWorld:
    """The designer, implementer and evaluators of one single run, answering from plain data.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994709 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        throughputs: Sequence[float | None] = (100.0,),
        passes: Sequence[bool] = (True,),
        plans: Sequence[str] | None = None,
        implementations: Sequence[str] | None = None,
        attribution_output: str = "",
        attribution_exit_code: int = 0,
        waits: Sequence[bool] = (),
        ends: Sequence[Ending] = ("wait",),
        polls: Sequence[int] = (0,),
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``throughputs`` is what candidates measure (None: the benchmark fails); a candidate's
        commit picks its entry. ``passes`` is the implementer's self-review verdict of its n-th
        reply, cycling; ``plans`` and ``implementations`` replace the designer's and the
        implementer's replies (default: valid ones). ``attribution_output`` is what the attribution
        command prints and ``attribution_exit_code`` how it ends.

        ``waits`` makes the n-th fresh implementer turn (cycling) submit an evaluation through
        the production bridge and wait for it; ``ends`` is how the n-th waiting turn ends once
        it validated its wait (with the waiting reply, the role's final result, or a reply that is
        not JSON); ``polls`` is how many polls the n-th evaluation job reports running (the last
        entry repeats). ``checkpoints`` answers the n-th checkpoint request ``succeeded``,
        ``failed`` or ``retryable`` (cycling). Any other option is a `SingleConfig` field.
        """
        config = single_config(**settings)
        self.setup = RunSetup(
            new_strategy=lambda: SingleStrategy(config=config),
            harness=Harness(
                registry=single_operation_registry(),
                facts=FACTS,
                limits=LIMITS,
                envelope_type=RunEnvelope[SingleStrategyState],
                requirements=requirements_for(config),
                lifecycle=frozenset({"suspend"}) if waits else frozenset(),
            ),
            schemas=reply_schemas(),
            # A turn that waits is resumed while another request runs, and the evaluation it
            # waits for is admitted to the shell by the loop's control poll.
            max_concurrent=2 if waits else 1,
            controlled=bool(waits),
        )
        self._throughputs = tuple(throughputs)
        self._planner = list(plans if plans is not None else self._designs(12))
        self._implementer = list(
            implementations
            if implementations is not None
            else [implemented(passed=passes[n % len(passes)]) for n in range(40)]
        )
        self._attribution = attribution_output
        self._attribution_exit_code = attribution_exit_code
        self._waits = tuple(waits)
        self._ends = tuple(ends)
        self._polls = tuple(polls)
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self._started = 0
        self._submitted = 0
        self._running: dict[str, int] = {}
        self._waiting: set[str] = set()
        self._readings: dict[EvidenceId, EvidenceReading] = {}

    @staticmethod
    def _designs(count: int) -> list[str]:
        return [plan_reply(f"h{n}") for n in range(count)]

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        return self._checkpoints(request, core)

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact | None:
        """Serve the evaluation tool to this host's turns that wait (a host without waits has none)."""
        if not self._waits:
            return None
        return WaitingTurns(shell, clock, FACTS, self._waiting)

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
                answer = self._turn(request)
            case _:
                answer = self._environment(request)
        return answer

    @staticmethod
    def _environment(request: Request) -> Answer:
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
                answer = Succeeded(revision=_revision(request.attempt.attempt_id.root))
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def _submit(self, request: SubmitMeasurement) -> Answer:
        assert request.request_id is not None
        resource = handle_for(request.request_id)
        self._running[resource] = self._polls[min(self._submitted, len(self._polls) - 1)]
        self._submitted += 1
        return Running(resource_id=ResourceId(root=resource))

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("single-")
        lease = _lease(request.turn.session.session_id)
        if isinstance(request, DispatchTurn) and role == "implementer" and self._waits:
            ordinal = self._started
            self._started += 1
            if self._waits[ordinal % len(self._waits)]:
                assert request.request_id is not None
                self._waiting.add(request.request_id.root)
                return Succeeded(output_json=self._ending(ordinal), resource_id=lease)
        queue = self._planner if role == "orchestrator" else self._implementer
        if not queue:
            return Unknown()
        reply = queue.pop(0)
        return Succeeded(output_json=reply, resource_id=lease)

    def _ending(self, ordinal: int) -> str:
        """The reply the waiting turn ends with."""
        match self._ends[ordinal % len(self._ends)]:
            case "wait":
                return WAITING
            case "invalid":
                return "not json"
            case "result":
                return self._implementer.pop(0)

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
        if kind is not EvidenceKind.BENCHMARK:
            return EvidenceReading(evidence_id=identifier, kind=kind, passed=True, stage=stage)
        value = BASELINE_THROUGHPUT if plan.purpose == "baseline" else self.headline(commit)
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
        typed = single_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self._readings[item.evidence_id] for item in typed.evidence),
                )
            )
        if isinstance(typed, CaptureCommand):
            return Succeeded(
                outcome=CommandCapture(
                    status="succeeded",
                    exit_code=self._attribution_exit_code,
                    output=self._attribution,
                    truncated=False,
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
