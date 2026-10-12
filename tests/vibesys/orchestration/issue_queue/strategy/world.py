"""The issue queue's world: what its roles reply, and the issue board they work on.

`IssueQueueWorld` is a vs-dst `World`. A test builds one from plain data, simulates it and
asserts on the run and on the board::

    world = IssueQueueWorld(implementer=[implemented()], judge=[judged()], perf=[evaluated()])
    run = simulate(world)

Keyword arguments that name no world option configure the strategy (`IssueQueueConfig`), so a
misspelling is refused by name. An executor reports what it saw (a reply, a write that landed)
and never core's conclusions. The board is a real `IssueBoardOwner` over a `FakeIssueTracker`,
so a crash copies it with the rest of the world and a scripted fault can lose an acknowledgement
after a write landed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.support.checkpoint_faults import FaultyCheckpoints
from tests.vibesys.orchestration.issue_queue.strategy._waiting import WAITING, WaitingTurns

from vibesys.orchestration.issue_queue.core_policy.api import (
    limits_for,
    reply_schemas,
    requirements_for,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    IssueQueueConfig,
    IssueQueueStrategy,
    IssueQueueStrategyState,
    RenderRoleArtifacts,
    plain_operation_registry,
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
    LifecycleCapability,
    Limits,
    ObservationStatus,
    ObserveOwnedJob,
    OwnedJob,
    RenderedArtifacts,
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
from vs_issue_tracker.api import FakeIssueTracker, LogProgress, MemoryProgressLog
from vs_runtime.api.core import IssueBoardOwner, handle_for

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_core.api import CoreState, Request
    from vs_dst.api import Enact
    from vs_issue_tracker.api import FaultPhase
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

FACTS = RunFacts(
    objective="serve the model",
    baseline=RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)
_KINDS = {
    "accuracy": EvidenceKind.CORRECTNESS,
    "benchmark": EvidenceKind.BENCHMARK,
    "profile": EvidenceKind.PROFILING,
}
_ROLES = {"implementer": "implementer", "judge": "judge", "perf-evaluator": "perf"}


# -- the replies the roles are asked to produce -------------------------------------------


def implemented(summary: str = "done", **extra: object) -> str:
    """An implementer result reply."""
    return json.dumps({"summary": summary, "files_touched": ["a.py"], "self_check": "ok", **extra})


def judged(*, passed: bool = True, bugs: tuple[str, ...] = (), **extra: object) -> str:
    """A judge result reply, optionally filing bugs."""
    return json.dumps(
        {
            "analysis": "analysis",
            "feedback": "" if passed else "fix it",
            "verdict": "pass" if passed else "fail",
            "new_issues": [
                {"type": "bug", "title": title, "description": f"## Background\n{title}"}
                for title in bugs
            ],
            **extra,
        }
    )


def evaluated(*titles: str, kinds: tuple[str, ...] = (), **extra: object) -> str:
    """A performance evaluator reply filing one issue per title."""
    return json.dumps(
        {
            "analysis": "analysis",
            "metrics": {
                "load_levels": [
                    {
                        "target_rate": 1.0,
                        "actual_rate": 1.0,
                        "num_requests": 4,
                        "num_completed": 4,
                        "num_failed": 0,
                        "duration": 1.0,
                        "throughput": {
                            "request_throughput": 4.0,
                            "token_throughput": 40.0,
                        },
                    }
                ],
                "extra": {},
            },
            "evaluator_feedback": ["measure more"],
            "new_issues": [
                {
                    "type": kinds[index] if index < len(kinds) else "perf",
                    "title": title,
                    "description": f"## Background\n{title}",
                }
                for index, title in enumerate(titles)
            ],
            "throughput_trend": "improved",
            "latency_trend": "mixed",
            **extra,
        }
    )


def generic_evaluated() -> str:
    """A domain-neutral performance evaluator reply."""
    return json.dumps(
        {
            "analysis": "The candidate processes more work per second.",
            "metrics": {
                "measurements": [
                    {
                        "name": "operations_per_second",
                        "value": 4.0,
                        "unit": "ops/s",
                        "workload": "default",
                    }
                ],
                "extra": {},
            },
            "evaluator_feedback": ["measure more"],
            "new_issues": [],
            "primary_metric_trend": "improved",
            "secondary_metric_trend": None,
        }
    )


# -- the strategy's setup -------------------------------------------------------------------


def issue_queue_config(**settings: object) -> IssueQueueConfig:
    """The strategy's config: two rounds and two attempts per issue unless ``settings`` say."""
    return IssueQueueConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            "max_issues_per_round": 4,
            **settings,
        }
    )


def issue_queue_limits(settings: IssueQueueConfig) -> Limits:
    """The run limits derived from ``settings``."""
    return limits_for(
        settings, queue_allowance=900.0, observe_interval=10.0, observe_backoff_cap=120.0
    )


def issue_queue_harness(
    settings: IssueQueueConfig,
    *,
    facts: RunFacts = FACTS,
    lifecycle: frozenset[LifecycleCapability] = frozenset(),
) -> Harness:
    """What hosts the strategy: its codec, the run's facts, limits and evidence requirements."""
    return Harness(
        registry=plain_operation_registry(),
        facts=facts,
        limits=issue_queue_limits(settings),
        envelope_type=RunEnvelope[IssueQueueStrategyState],
        requirements=requirements_for(),
        lifecycle=lifecycle,
    )


# -- the board ------------------------------------------------------------------------------


class _Texts:
    """Board texts that name their input, so a test can tell which template produced them."""

    def issue_body(self, template: str) -> str:
        """The description of an issue filed from ``template``."""
        return f"rendered {template}"

    def progress_entry(self, request: LogProgress) -> str:
        """The progress entry of a recorded turn."""
        return f"{request.step} {request.iteration}"


@dataclass
class ScheduledFaults:
    """The tracker fails around the calls ``schedule`` names, counting every write's two phases.

    The n-th call of the tracker's fault hook (a write's ``before`` phase, then its ``after``
    phase) raises when ``schedule[n]`` is that call's phase. A ``before`` fault means the write
    did not happen; ``after`` that it landed and its acknowledgement was lost. Reads never fail
    and are not counted.
    """

    schedule: dict[int, FaultPhase]
    calls: int = 0

    def __call__(self, operation: str, phase: FaultPhase) -> None:
        """Raise when the schedule says this call fails."""
        if operation in {"get", "list"}:
            return
        number = self.calls
        self.calls += 1
        if self.schedule.get(number) == phase:
            message = f"tracker {phase} {operation}"
            raise ConnectionError(message)


def _lease(session_id: SessionId) -> ResourceId:
    """The lease an executor holds for a session: the same name from ensure through close."""
    return ResourceId(root=f"lease:{session_id.root}")


def revision_of(attempt: str) -> RevisionRef:
    """The commit the workspace executor retains for an attempt: fixed by the attempt."""
    return RevisionRef.of_git_commit(hashlib.sha256(attempt.encode()).hexdigest()[:40])


class IssueQueueWorld:
    """The roles of one issue-queue run and the board they work on, answering from plain data.

    A reply is handed out once, in the order requests are authorized, so a copy of the world
    taken mid-run continues from where the run stood. A role with no reply left reports
    nothing (an unknown effect).
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994710 [PLR0913]; each option is one independent fact of the scripted world, and a test names the ones it varies.
        # > Grouping them in a holder would make every test build the holder before the world.
        self,
        *,
        implementer: Sequence[str | Answer] = (),
        judge: Sequence[str | Answer] = (),
        perf: Sequence[str | Answer] = (),
        accurate: bool = True,
        silent_official: bool = False,
        unchanged: Sequence[str] = (),
        tracker_faults: Mapping[int, FaultPhase] | None = None,
        record_requests: bool = False,
        waits: Sequence[bool] = (),
        ends: Sequence[str] = ("wait",),
        roles: Sequence[str] = ("implementer", "judge"),
        delays: Sequence[int] = (0,),
        polls: int = 0,
        checkpoints: Sequence[str] = (),
        **settings: object,
    ) -> None:
        """Script a run.

        ``implementer``, ``judge`` and ``perf`` are the replies each role gives, in order: the
        agent's JSON, or an `Answer` for a turn that ends another way. ``accurate`` is whether
        the trusted accuracy gate passes; ``silent_official`` makes the final (official)
        measurement never report. ``unchanged`` names the attempts whose turns change nothing:
        the workspace executor retains the revision the attempt started from, as a snapshot of
        an unchanged tree is. ``tracker_faults`` maps the n-th call of the tracker's fault hook
        to the phase it fails in (see `ScheduledFaults`). ``record_requests`` keeps every
        request the executors received in ``requests``.

        ``waits`` makes the n-th fresh turn of a role in ``roles`` (cycling) submit an
        evaluation through the production bridge and wait for it; ``ends`` is how the n-th
        waiting turn ends once it validated its wait (``"wait"``: the waiting reply, ``"result"``:
        the role's final result, ``"invalid"``: a reply that is not JSON); ``delays`` is how many
        event-loop yields the n-th turn takes; ``polls`` is how many polls each evaluation job
        reports running; ``checkpoints`` answers the n-th checkpoint request ``succeeded``,
        ``failed`` or ``retryable`` (cycling). Any other option is an `IssueQueueConfig` field.
        """
        self.config = issue_queue_config(**settings)
        config = self.config
        self.setup = RunSetup(
            new_strategy=lambda: IssueQueueStrategy(config=config),
            harness=issue_queue_harness(
                config, lifecycle=frozenset({"suspend"}) if waits else frozenset()
            ),
            schemas=reply_schemas(config),
            # A turn that waits is resumed while another request runs, and the evaluation it
            # waits for is admitted to the shell by the loop's control poll.
            max_concurrent=2 if waits else 1,
            controlled=bool(waits),
        )
        self.tracker = FakeIssueTracker(
            faults=None if tracker_faults is None else ScheduledFaults(dict(tracker_faults))
        )
        self.progress = MemoryProgressLog()
        self.requests: list[Request] = []
        self._record = record_requests
        self._replies = {
            "implementer": list(implementer),
            "judge": list(judge),
            "perf": list(perf),
        }
        self._accurate = accurate
        self._silent_official = silent_official
        self._unchanged = frozenset(unchanged)
        self._waits = tuple(waits)
        self._ends = tuple(ends)
        self._roles = tuple(roles)
        self._delays = tuple(delays)
        self._polls = polls
        self._checkpoints = FaultyCheckpoints(self._answer, tuple(checkpoints))
        self._started = 0
        self._waiting: set[str] = set()
        self._running: dict[str, int] = {}
        self._bases: dict[str, RevisionRef] = {}
        self._ranks: dict[str, int] = {}

    # -- what a test reads back -------------------------------------------------------------

    def board(self) -> dict[int, tuple[str, int]]:
        """Every issue's status and attempts, by number."""
        return {issue.id: (issue.status.value, issue.attempts) for issue in self.tracker.list()}

    def titles(self) -> list[str]:
        """The title of every issue, in the order the board lists them."""
        return [issue.title for issue in self.tracker.list()]

    def history(self) -> list[tuple[object, ...]]:
        """Every issue with its status, attempts and the actions recorded on it."""
        return [
            (issue.id, issue.status.value, issue.attempts, [e.action for e in issue.history])
            for issue in self.tracker.list()
        ]

    def headline(self, commit: str) -> float | None:
        """The throughput of a candidate in this world: its rank among the revisions retained.

        A later revision was built on the earlier ones, so it is the better candidate (the
        issue queue adopts its last tip).
        """
        rank = self._ranks.get(commit)
        return None if rank is None else float(rank + 1)

    # -- the executors ------------------------------------------------------------------------

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the executors report for ``request``."""
        if self._record:
            self.requests.append(request)
        return self._checkpoints(request, core)

    def attach(self, shell: CoreRuntime[Any], clock: VirtualClock) -> Enact | None:
        """Serve the evaluation tool to this host's turns that wait (a host without waits has none)."""
        if not self._waits:
            return None
        return WaitingTurns(shell, clock, self.config, FACTS, self._waiting, self._delays)

    def _answer(self, request: Request, core: CoreState) -> Answer:
        """What an executor that needs no view beyond ``core`` reports for ``request``."""
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

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        """An operation answered in process: a board operation is performed by the real owner."""
        typed = plain_operation_registry().decode(request.operation)
        if isinstance(typed, RenderRoleArtifacts):
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
        owner = IssueBoardOwner(self.tracker, self.progress, _Texts())
        try:
            return Succeeded(outcome=owner.perform(typed))
        except ConnectionError:
            # The tracker failed around the write: whether it landed is for core to find out.
            return Unknown()

    def _submit(self, request: SubmitMeasurement) -> Answer:
        """A job is named by its request, as the agent evaluation bridge names the handle."""
        if self._silent_official and request.plan.purpose == "official":
            return Unknown()
        assert request.request_id is not None
        return Running(resource_id=ResourceId(root=handle_for(request.request_id)))

    def _environment(self, request: Request) -> Answer:
        """What the workspace, session and adoption executors report: they need no script."""
        answer: Answer = Succeeded()
        match request:
            case EnsureWorkspace():
                self._bases[request.attempt.attempt_id.root] = request.plan.base
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
                attempt = request.attempt.attempt_id.root
                kept = self._bases[attempt] if attempt in self._unchanged else None
                answer = Succeeded(revision=self._retained(kept or revision_of(attempt)))
            case RetainRevision():
                answer = Succeeded(revision=request.revision)
            case AdoptRevision() | VerifyAdoption():
                answer = Succeeded(revision=request.selection.revision)
        return answer

    def _retained(self, revision: RevisionRef) -> RevisionRef:
        self._ranks.setdefault(revision.revision_id.root, len(self._ranks))
        return revision

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = _ROLES[request.turn.session.role_id.root.removeprefix("plain-")]
        lease = _lease(request.turn.session.session_id)
        if isinstance(request, DispatchTurn) and role in self._roles and self._waits:
            ordinal = self._started
            self._started += 1
            if self._waits[ordinal % len(self._waits)]:
                assert request.request_id is not None
                self._waiting.add(request.request_id.root)
                return Succeeded(output_json=self._ending(ordinal, role), resource_id=lease)
        return self._reply(role, lease)

    def _reply(self, role: str, lease: ResourceId) -> Answer:
        """The next reply of ``role``; nothing is reported once the script has run out."""
        queue = self._replies[role]
        if not queue:
            return Unknown()
        reply = queue.pop(0)
        if not isinstance(reply, str):
            return reply
        return Succeeded(output_json=reply, resource_id=lease)

    def _ending(self, ordinal: int, role: str) -> str:
        """The reply the waiting turn ends with."""
        match self._ends[ordinal % len(self._ends)]:
            case "result":
                answer = self._reply(role, ResourceId(root="unused"))
                assert isinstance(answer, Succeeded)
                assert answer.output_json is not None
                return answer.output_json
            case "invalid":
                return "not json"
            case _:
                return WAITING

    def _observe(self, request: ObserveOwnedJob, core: CoreState) -> Answer:
        """A trusted measurement job that ends with the verdict the world was set to."""
        resource = request.resource_id.root
        left = self._running.setdefault(resource, self._polls)
        if left > 0:
            self._running[resource] = left - 1
            return Running(resource_id=request.resource_id)
        job = next(
            item
            for item in core.evaluation.jobs
            if isinstance(item, OwnedJob) and item.resource_id == request.resource_id
        )
        plan = job.plan
        assert isinstance(plan.candidate, RevisionRef)
        commit = plan.candidate.revision_id.root
        status = ObservationStatus.SUCCEEDED if self._accurate else ObservationStatus.FAILED
        outcome = EvaluationStageOutcome.PASSED if self._accurate else EvaluationStageOutcome.FAILED
        refs = []
        stages = []
        for stage in plan.stages:
            refs.append(
                EvidenceRef(
                    evidence_id=EvidenceId(root=f"{stage.stage_id}:{plan.purpose}:{commit}"),
                    kind=(
                        EvidenceKind.LOCAL_VALIDATION
                        if plan.purpose == "local-validation"
                        else _KINDS[stage.stage_id]
                    ),
                    purpose=plan.purpose,
                    scope=job.scope,
                    source_request=job.submission_id,
                    candidate=plan.candidate,
                    observation_sequence=0,
                    evaluator_digest=plan.evaluator_digest,
                    workload_digest=plan.workload_digest,
                    environment_digest=plan.environment_digest,
                    provenance="trusted",
                    status=status,
                )
            )
            stages.append(EvaluationStageResult(stage_id=stage.stage_id, outcome=outcome))
        return Succeeded(
            resource_id=request.resource_id,
            evidence=tuple(refs),
            facts=EvaluationTerminalFacts(stages=tuple(stages), accuracy_passed=self._accurate),
        )


__all__ = [
    "FACTS",
    "IssueQueueWorld",
    "ScheduledFaults",
    "evaluated",
    "generic_evaluated",
    "implemented",
    "issue_queue_config",
    "issue_queue_harness",
    "issue_queue_limits",
    "judged",
    "revision_of",
]
