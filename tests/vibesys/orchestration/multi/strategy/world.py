"""The multi strategy's world: what its agents reply and what its benchmarks measure.

`MultiWorld` is a vs-dst `World`. A test builds one from plain data, simulates it and asserts
on the run::

    world = MultiWorld(plans=[plan_reply("h1")], max_rounds=1)
    run = simulate(world)

Keyword arguments that name no world option configure the strategy (`MultiConfig`), so a
misspelling is refused by name. An executor reports what it saw (a job accepted, a stage
passed, a reply) and never core's conclusions; evidence identities derive from the job's plan, so
a reading and the evidence it decodes always agree.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.vibesys.orchestration.multi.strategy._waiting import WaitingTurns

from vibesys.orchestration.multi.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.multi.strategy.api import (
    CaptureCommand,
    CommandCapture,
    EvidenceReading,
    EvidenceReadings,
    InterpretEvidence,
    LocalValidationReport,
    MetricRow,
    MultiConfig,
    MultiStrategy,
    MultiStrategyState,
    ParentVerification,
    PartialRow,
    RenderedArtifacts,
    RenderRoleArtifacts,
    ValidateLocal,
    VerifyParentRevision,
    multi_operation_registry,
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
    from collections.abc import Mapping, Sequence

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
LIMITS = Limits(max_attempts=4, max_turns=200, max_parallel=1, max_retries=3, max_refunds=4)
BASELINE_THROUGHPUT = 50.0
"""What the input measures; a candidate measures what ``throughputs`` says."""
RECIPE = "validation/recipes.json"
"""A validation recipe an implementer reply can name (see `implemented`)."""
PROFILER: Mapping[str, Any] = {
    "profiler_id": "linux_cpu",
    "profiler_support_name": "linux_cpu_profiler",
    "profiler_mcp_name": "vibesys-linux-cpu-profiler",
}
"""Strategy settings that enable the advisory profiler."""

type Ending = Literal["wait", "result", "invalid"]
ENDINGS: tuple[Ending, ...] = ("wait", "result", "invalid")
"""How a turn that validated its wait can end (see `MultiWorld`'s ``ends``)."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_WAITING_REPLY = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
_ATTRIBUTION = json.dumps(
    {
        "version": 1,
        "cost_unit": "ms",
        "components": [
            {"name": "gemm", "cost": 8.0, "share": 0.8, "evidence": ["a"]},
            {"name": "io", "cost": 2.0, "share": 0.2, "evidence": []},
        ],
    }
)
_PASSED = LocalValidationReport(
    status="succeeded",
    passed=True,
    report=ArtifactRef(artifact_id=ArtifactId(root="validation:report"), digest="report"),
)


def pre_round_reply(*, need_profile: bool = False, focus: str = "") -> str:
    """The routing decision: whether fresh profile evidence is wanted."""
    return json.dumps(
        {"need_profile": need_profile, "profile_focus": focus, "reasoning": "because"}
    )


def profiler_reply(**extra: object) -> str:
    """The profiler's findings."""
    return json.dumps(
        {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it", **extra}
    )


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


def implemented(outcome: str = "nominated", **extra: object) -> str:
    """An implementer reply."""
    return json.dumps(
        {
            "summary": "did it",
            "expected_behavior": "faster",
            "hypothesis_outcome": outcome,
            **extra,
        }
    )


def reviewed(*, passed: bool = True, **extra: object) -> str:
    """A judge reply."""
    return json.dumps(
        {
            "analysis": "checked",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            **extra,
        }
    )


def multi_config(**settings: object) -> MultiConfig:
    """The strategy's config: two rounds unless ``settings`` say otherwise."""
    return MultiConfig.model_validate(
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


class MultiWorld:
    """The designer, profiler, implementer, judge and evaluators of one multi run.

    They answer from plain data. A reply is handed out once, in the order requests are
    authorized, so a copy of the world taken mid-run continues from where the run stood.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-271828 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        throughputs: Sequence[float | None] = (100.0,),
        benchmark_failures: int = 0,
        passes: Sequence[bool] = (True,),
        pre_rounds: Sequence[str] | None = None,
        profiles: Sequence[str] | None = None,
        plans: Sequence[str] | None = None,
        implementations: Sequence[str] | None = None,
        reviews: Sequence[str] | None = None,
        validations: Sequence[LocalValidationReport] = (_PASSED,),
        profiling: bool = False,
        attribution_output: str | None = None,
        attribution_exit_code: int = 0,
        waits: Sequence[bool] = (),
        wait_roles: Sequence[str] = ("implementer", "judge"),
        ends: Sequence[Ending] = ("wait",),
        polls: Sequence[int] = (0,),
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``throughputs`` is what candidates measure (None: the benchmark fails); a candidate's
        commit picks its entry. ``benchmark_failures`` makes the first n candidate benchmarks
        fail whatever ``throughputs`` says. ``passes`` is the judge's verdict of its n-th reply,
        cycling; ``pre_rounds``, ``profiles``, ``plans``, ``implementations`` and ``reviews``
        replace the routing turn's, the profiler's, the designer's, the implementer's and the
        judge's replies (default: valid ones). ``validations`` is what the trusted run of the
        n-th validation recipe reports (the last repeats). ``profiling`` makes the routing turn
        ask for a profile and the attribution command print a valid result; ``attribution_output``
        and ``attribution_exit_code`` say what the command prints and how it ends instead.

        ``waits`` makes the n-th fresh turn of the ``wait_roles`` (cycling) submit an evaluation
        through the production bridge and wait for it; ``ends`` is how the n-th waiting turn
        ends once it validated its wait (with the waiting reply, the role's final result, or a
        reply that is not JSON); ``polls`` is how many polls the n-th evaluation job reports
        running (the last entry repeats). ``checkpoints`` answers the n-th checkpoint request
        ``succeeded``, ``failed`` or ``retryable`` (cycling). Any other option is a
        `MultiConfig` field.
        """
        config = multi_config(**settings)
        self.setup = RunSetup(
            new_strategy=lambda: MultiStrategy(config=config),
            harness=Harness(
                registry=multi_operation_registry(),
                facts=FACTS,
                limits=LIMITS,
                envelope_type=RunEnvelope[MultiStrategyState],
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
        self._benchmark_failures = benchmark_failures
        self._queues = {
            "preround": list(
                pre_rounds
                if pre_rounds is not None
                else [pre_round_reply(need_profile=profiling)] * 8
            ),
            "profiler": list(profiles if profiles is not None else [profiler_reply()] * 8),
            "orchestrator": list(
                plans if plans is not None else [plan_reply(f"h{n}") for n in range(12)]
            ),
            "implementer": list(
                implementations if implementations is not None else [implemented()] * 40
            ),
            "judge": list(
                reviews
                if reviews is not None
                else [reviewed(passed=passes[n % len(passes)]) for n in range(40)]
            ),
        }
        self._validations = tuple(validations)
        self._attribution = (
            attribution_output
            if attribution_output is not None
            else (_ATTRIBUTION if profiling else "")
        )
        self._attribution_exit_code = attribution_exit_code
        self._waits = tuple(waits)
        self._wait_roles = tuple(wait_roles)
        self._ends = tuple(ends)
        self._polls = tuple(polls)
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self._started = 0
        self._submitted = 0
        self._validated = 0
        self._running: dict[str, int] = {}
        self._waiting: set[str] = set()
        self._readings: dict[EvidenceId, EvidenceReading] = {}

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
        role = request.turn.session.role_id.root.removeprefix("multi-")
        lease = _lease(request.turn.session.session_id)
        if isinstance(request, DispatchTurn) and role in self._wait_roles and self._waits:
            ordinal = self._started
            self._started += 1
            if self._waits[ordinal % len(self._waits)]:
                assert request.request_id is not None
                self._waiting.add(request.request_id.root)
                return Succeeded(output_json=self._ending(request, ordinal), resource_id=lease)
        return self._reply(request, lease)

    def _reply(self, request: DispatchTurn | ResumeSessionTurn, lease: ResourceId) -> Answer:
        """The next scripted reply of the role (the designer's two subjects have one queue each)."""
        role = request.turn.session.role_id.root.removeprefix("multi-")
        if role == "orchestrator" and ":preround:" in request.turn.invocation_id.root:
            role = "preround"
        queue = self._queues[role]
        if not queue:
            return Unknown()
        return Succeeded(output_json=queue.pop(0), resource_id=lease)

    def _ending(self, request: DispatchTurn, ordinal: int) -> str:
        """The reply the waiting turn ends with."""
        match self._ends[ordinal % len(self._ends)]:
            case "wait":
                return _WAITING_REPLY
            case "invalid":
                return "not json"
            case "result":
                answer = self._reply(request, _lease(request.turn.session.session_id))
                assert isinstance(answer, Succeeded)
                assert answer.output_json is not None
                return answer.output_json

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
        if plan.purpose != "baseline" and self._benchmark_failures:
            self._benchmark_failures -= 1
            value = None
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
        typed = multi_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self._readings[item.evidence_id] for item in typed.evidence),
                )
            )
        if isinstance(typed, ValidateLocal):
            report = self._validations[min(self._validated, len(self._validations) - 1)]
            self._validated += 1
            return Succeeded(outcome=report)
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
