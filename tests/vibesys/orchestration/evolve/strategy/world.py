"""The evolve strategy's world: what its mutators, judges and profilers reply and what is measured.

`EvolveWorld` is a vs-dst `World`. A test builds one from plain data, simulates it and asserts
on the run::

    world = EvolveWorld(throughputs=(120.0, 80.0), max_generations=1)
    run = simulate(world)

Keyword arguments that name no world option configure the strategy (`EvolveConfig`), so a
misspelling is refused by name. An executor reports what it saw (a job accepted, a stage
passed, a reply) and never core's conclusions; evidence identities derive from the job's plan, so
a reading and the evidence it decodes always agree.

A reply comes from the first of three sources: the scripted population (``population_seed``: a
pure function of a child's name), the role's reply list when the test gave one, or a reply
generated from the child's name, so a whole world of candidates is a function of their names.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.vibesys.orchestration.evolve import world as legacy
from tests.vibesys.orchestration.evolve.strategy._waiting import (
    ENVELOPED_WAITING,
    WAITING,
    WaitingTurns,
)

from vibesys.orchestration.evolve.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.evolve.strategy.api import (
    EvidenceReading,
    EvidenceReadings,
    EvolveConfig,
    EvolveStrategy,
    EvolveStrategyState,
    ExportPatch,
    InterpretEvidence,
    MetricRow,
    ParentVerification,
    PartialRow,
    PatchExport,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    evolve_operation_registry,
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
    component,
)
from vs_core.testing.drive import Answer, Failed, Harness, Running, Succeeded, Unknown
from vs_dst.api import RunSetup, simulate
from vs_runtime.api.core import handle_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreState, MeasurementPlan, Request, RequestId, Scope
    from vs_dst.api import Enact, SimulatedRun
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

FACTS = RunFacts(
    objective="make it faster",
    baseline=RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)

type Ending = Literal["wait", "wait_enveloped", "result", "invalid"]
ENDINGS: tuple[Ending, ...] = ("wait", "wait_enveloped", "result", "invalid")
"""How a turn that validated its wait can end (see `EvolveWorld`'s ``ends``)."""

_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
}


def mutated(summary: str = "did it", **extra: object) -> str:
    """A mutator reply."""
    return json.dumps(
        {"summary": summary, "hypothesis": "because", "expected_behavior": "faster", **extra}
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


def profiled(**extra: object) -> str:
    """The profiler's findings."""
    return json.dumps(
        {"analysis": "analysis", "bottlenecks": "hot loop", "suggestions": "fuse it", **extra}
    )


def evolve_config(**settings: object) -> EvolveConfig:
    """The strategy's config: two generations of two children unless ``settings`` say otherwise."""
    return EvolveConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_generations": 2,
            "children_per_generation": 2,
            "k_top_inspirations": 1,
            "k_random_inspirations": 1,
            "selection_temperature": 1.0,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 2,
            "seed": 7,
            **settings,
        }
    )


def commit_of(attempt: str) -> str:
    """The commit the workspace executor retains for an attempt's one checkpoint."""
    return hashlib.sha256(attempt.encode()).hexdigest()[:40]


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def _child_key(subject: str) -> str:
    """The child (`g2.c1`, `bootstrap.1`) a turn's invocation or session names."""
    return subject.rsplit(".", 1)[0]


class EvolveWorld:
    """The mutators, judges, profilers and evaluators of one evolve run, answering from plain data.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-604117 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        throughputs: Sequence[float | None] = (100.0,),
        unit: str | None = "tokens/s",
        passes: Sequence[bool] = (True,),
        population_seed: int | None = None,
        mutations: Sequence[str] | None = None,
        reviews: Sequence[str] | None = None,
        profiles: Sequence[str] | None = None,
        enveloped: bool = False,
        unmeasurable: bool = False,
        polls: Sequence[int] = (0,),
        waits: Sequence[bool] = (),
        ends: Sequence[Ending] = ("wait",),
        wait_roles: Sequence[str] = ("mutator",),
        submit_only: Sequence[str] = (),
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``throughputs`` is what candidates measure (None: the benchmark fails), a candidate's
        commit picks its entry, and ``unit`` is the headline's declared unit. ``passes`` is the
        judge's verdict for a child, picked by its name. ``population_seed`` replaces all of
        these with the scripted population of that seed (`tests.vibesys.orchestration.evolve.world`):
        each child's verdict, throughput, second metric and summary. ``mutations``, ``reviews``
        and ``profiles`` replace a role's replies by a list handed out in order (when it runs
        out the turn gets no answer); ``enveloped`` wraps every reply in the ``reply`` envelope.
        ``unmeasurable`` makes every official measurement end without evidence. ``polls`` is how
        many polls the n-th evaluation job reports running (the last entry repeats).

        ``waits`` makes the n-th fresh turn (cycling) of a role in ``wait_roles`` submit an
        evaluation through the production bridge and wait for it; ``ends`` is how the n-th
        waiting turn ends once it validated its wait (the waiting reply, bare or enveloped, the
        role's final result, or a reply that is not JSON). A fresh turn of a role in
        ``submit_only`` submits and ends plainly, leaving the job running. ``checkpoints``
        answers the n-th checkpoint request ``succeeded``, ``failed`` or ``retryable``
        (cycling). Any other option is an `EvolveConfig` field.
        """
        config = evolve_config(**settings)
        self._bridge = bool(waits) or bool(submit_only)
        self.setup = RunSetup(
            new_strategy=lambda: EvolveStrategy(config=config),
            harness=Harness(
                registry=evolve_operation_registry(),
                facts=FACTS,
                limits=_limits(config),
                envelope_type=RunEnvelope[EvolveStrategyState],
                requirements=requirements_for(config),
                lifecycle=frozenset({"suspend"}) if self._bridge else frozenset(),
            ),
            schemas=reply_schemas(),
            # A turn that waits is resumed while another request runs, and the evaluation it
            # waits for is admitted to the shell by the loop's control poll.
            max_concurrent=max(2, config.max_parallelism)
            if self._bridge
            else config.max_parallelism,
            controlled=self._bridge,
        )
        self._throughputs = tuple(throughputs)
        self._unit = unit
        self._passes = tuple(passes)
        self._population = _population(population_seed, config)
        self._population_seed = population_seed
        self._replies: dict[str, list[str] | None] = {
            "mutator": None if mutations is None else list(mutations),
            "judge": None if reviews is None else list(reviews),
            "profiler": None if profiles is None else list(profiles),
        }
        self._enveloped = enveloped
        self._unmeasurable = unmeasurable
        self._polls = tuple(polls)
        self._waits = tuple(waits)
        self._ends = tuple(ends)
        self._wait_roles = tuple(wait_roles)
        self._submit_only = tuple(submit_only)
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self._started = 0
        self._submitted = 0
        self._running: dict[str, int] = {}
        self._readings: dict[EvidenceId, EvidenceReading] = {}
        self._acting: dict[str, bool] = {}
        self.asked: list[tuple[str, str, dict[str, Any]]] = []
        """(attempt, role, reply) of every submit the tool server answered, errors included."""

    def headline(self, commit: str) -> float | None:
        """What the candidate of ``commit`` measures (a pure function of its commit)."""
        if self._population is not None:
            return self._population[commit].throughput
        return self._throughputs[int(commit, 16) % len(self._throughputs)]

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        return self._checkpoints(request, core)

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact | None:
        """Serve the evaluation tool to this host's turns that use it (a host without has none)."""
        if not self._bridge:
            return None
        return WaitingTurns(shell, clock, FACTS, self._acting, self.asked)

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
                # One commit per attempt, whatever its turns did: production snapshotting
                # records the same revision when a turn changed nothing.
                answer = Succeeded(
                    revision=RevisionRef.of_git_commit(commit_of(request.attempt.attempt_id.root))
                )
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

    # -- agents -----------------------------------------------------------

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        session = request.turn.session
        role = session.role_id.root.removeprefix("evolve-")
        # `inv:<role>:<key>.<role>:<serial>`
        _, _, subject, _ = request.turn.invocation_id.root.split(":")
        key = _child_key(subject)
        lease = _lease(session.session_id)
        if isinstance(request, DispatchTurn):
            assert request.request_id is not None
            if role in self._submit_only:
                self._acting[request.request_id.root] = False
            elif role in self._wait_roles and self._waits:
                ordinal = self._started
                self._started += 1
                if self._waits[ordinal % len(self._waits)]:
                    self._acting[request.request_id.root] = True
                    reply = self._ending(ordinal, role, key)
                    if reply is None:
                        return Unknown()
                    return Succeeded(output_json=reply, resource_id=lease)
        reply = self._reply(role, key)
        if reply is None:
            return Unknown()
        return Succeeded(output_json=reply, resource_id=lease)

    def _ending(self, ordinal: int, role: str, key: str) -> str | None:
        """The reply the waiting turn ends with."""
        match self._ends[ordinal % len(self._ends)]:
            case "wait":
                return WAITING
            case "wait_enveloped":
                return ENVELOPED_WAITING
            case "invalid":
                return "not json"
            case "result":
                return self._reply(role, key)

    def _reply(self, role: str, key: str) -> str | None:
        """What ``role`` answers for the child ``key``; None when its scripted replies ran out."""
        if self._population_seed is not None:
            return self._scripted(role, key)
        queue = self._replies[role]
        if queue is None:
            reply = self._generated(role, key)
        elif queue:
            reply = queue.pop(0)
        else:
            return None
        return json.dumps({"reply": json.loads(reply)}) if self._enveloped else reply

    def _generated(self, role: str, key: str) -> str:
        """A child's reply as a pure function of its name, independent of its siblings' order."""
        if role == "mutator":
            return mutated(f"change of {key}")
        if role == "judge":
            return reviewed(passed=self._passes[sum(map(ord, key)) % len(self._passes)])
        return profiled()

    def _scripted(self, role: str, key: str) -> str:
        assert self._population_seed is not None
        child = legacy.child(self._population_seed, key)
        if role == "mutator":
            return mutated(child.summary)
        if role == "judge":
            return reviewed(
                passed=child.judge_passes, feedback="" if child.judge_passes else f"fails {key}"
            )
        return profiled(metrics={"cache_hit": child.cache_hit})

    # -- evaluator --------------------------------------------------------

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
        if self._unmeasurable and plan.purpose == "official":
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
        value = self.headline(commit)
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
            metrics=(
                MetricRow(name="throughput", value=value, direction="max", unit=self._unit),
                *self._second_metric(commit),
            ),
        )

    def _second_metric(self, commit: str) -> tuple[MetricRow, ...]:
        """The further row the scripted population reports with the throughput."""
        if self._population is None:
            return ()
        return (MetricRow(name="ttft_ms", value=self._population[commit].ttft_ms, direction="min"),)

    # -- registered operations -------------------------------------------

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = evolve_operation_registry().decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        if isinstance(typed, InterpretEvidence):
            return Succeeded(
                outcome=EvidenceReadings(
                    status="succeeded",
                    readings=tuple(self._readings[item.evidence_id] for item in typed.evidence),
                )
            )
        if isinstance(typed, ExportPatch):
            return Succeeded(
                outcome=PatchExport(
                    status="succeeded", patch=f"patch for {typed.revision.revision_id.root}"
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


def run_to_end(world: EvolveWorld) -> SimulatedRun:
    """Simulate ``world``, which must reach its terminal state without a halt or a liveness break."""
    run = simulate(world)
    assert run.finished
    assert run.halted is None
    assert not run.liveness_violations
    return run


def _limits(config: EvolveConfig) -> Limits:
    """Generous bounds that still fit the run's own budgets."""
    attempts = config.total_attempts
    return Limits(
        max_attempts=attempts,
        max_turns=60 * attempts,
        max_parallel=config.max_parallelism,
        max_retries=4,
        max_refunds=attempts,
    )


def _population(seed: int | None, config: EvolveConfig) -> dict[str, legacy.Child] | None:
    """The scripted children by the commit the workspace retains for them, or None."""
    if seed is None:
        return None
    names = [
        *(legacy.bootstrap_name(number) for number in range(1, config.bootstrap_max_attempts + 1)),
        *legacy.generation_names(config.max_generations, config.children_per_generation),
    ]
    return {commit_of(f"attempt:{component(name)}:1"): legacy.child(seed, name) for name in names}
