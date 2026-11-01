"""The skeleton search as a subject for the delivery-fault tests.

The delivery tests (`test_delivery_faults.py`, `test_request_kinds.py`) drive the skeleton search
(`tests/support/skeleton_strategy.py`) through core with a scripted far side. This module is that
far side: the harness that hosts the search,
the world that answers every request, and the wrappers that make an answer unknown, retryable or
silent.

The skeleton is the one subject because the guarantees checked here belong to the shell and core,
not to a search. A search that is correct on core's contract inherits them (see
`docs/contributing/flows.md`).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from tests.support.dynamic_script import FACTS, Executors, lease
from tests.support.skeleton_strategy import (
    ATTEMPT,
    OPERATIONS,
    Implementation,
    Interpret,
    Readings,
    SkeletonConfig,
    SkeletonStrategy,
    Verify,
    skeleton,
)

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    ExecuteRegisteredOperation,
    FlowState,
    MeasurementFailure,
    ObserveOwnedJob,
    OwnedJob,
    ParentVerification,
    RenderedArtifacts,
    ResourceId,
    ResumeSessionTurn,
    RunEnvelope,
    SubmitMeasurement,
)
from vs_core.api.testing import sample_limits
from vs_core.testing.builders import sample_retries
from vs_core.testing.drive import (
    Answer,
    Failed,
    Harness,
    Retryable,
    Succeeded,
    Unknown,
)
from vs_runtime.api.testing import Launch

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request

COMMIT = hashlib.sha256(ATTEMPT.attempt_id.root.encode()).hexdigest()[:40]
"""The commit the scripted implementer reports: the one core's snapshot of the attempt makes."""

DEADLINE = 15_000.0
"""When a run that nothing ends gives up (the run clock); short, so a hang is a quick failure."""

MEASUREMENT_OUTCOMES = ("succeeded", "workload_failure", "infrastructure_failure", "lost")
TURN_OUTCOMES = ("replies", "unparsable", "lost")


def launch() -> Launch[SkeletonConfig]:
    """A fresh launch of the skeleton search."""
    return Launch(skeleton, SkeletonStrategy().config)


def harness(**overrides: object) -> Harness:
    """What hosts the skeleton search on the real core."""
    hosting = Harness(
        registry=OPERATIONS.registry(),
        facts=FACTS,
        limits=sample_limits(
            request_retries=sample_retries(2),
            observe_interval=1.0,
            observe_backoff_cap=1.0,
            max_adoption_rounds=2,
            # Longer than a host's lease, so a request in flight when a host dies is still wanted.
            reconciliation_bound=5000.0,
        ),
        envelope_type=RunEnvelope[FlowState],
        schemas=skeleton.reply_schemas,
        deadline_at=DEADLINE,
        shell_events=True,
    )
    return replace(hosting, **overrides)


class SkeletonScript(Executors):
    """The far side of a skeleton run: the implementer commits, every measurement passes."""

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        return Succeeded(
            output_json=Implementation(commit=COMMIT).model_dump_json(),
            resource_id=lease(request.turn.session.session_id),
        )

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        typed = OPERATIONS.registry().decode(request.operation)
        if isinstance(typed, Interpret):
            return Succeeded(outcome=Readings(status="succeeded"))
        if isinstance(typed, Verify):
            return Succeeded(outcome=ParentVerification(status="succeeded", verified=True))
        prompt = ArtifactRef(
            artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"), digest="prompt"
        )
        return Succeeded(outcome=RenderedArtifacts(status="succeeded", prompts=(prompt,)))


@dataclass(frozen=True)
class Outcomes:
    """How each measurement submission and agent turn of a run goes, in order of occurrence.

    The last outcome of a list repeats; an empty list means every one succeeds.
    """

    measurements: tuple[str, ...] = ()
    turns: tuple[str, ...] = ()


def _nth(outcomes: tuple[str, ...], position: int, default: str) -> str:
    return default if not outcomes else outcomes[min(position, len(outcomes) - 1)]


class Faulted:
    """A world whose measurements and turns go the way `Outcomes` says, over the scripted world."""

    def __init__(
        self, outcomes: Outcomes | None = None, *, base: SkeletonScript | None = None
    ) -> None:
        """Layer ``outcomes`` over ``base`` (a plain scripted world by default)."""
        self._base = base or SkeletonScript()
        self._outcomes = outcomes or Outcomes()
        self._submitted = 0
        self._asked = 0
        self._infrastructure: set[str] = set()
        self.executed: Counter[str] = Counter()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """Answer from the generated outcome when there is one, else from the scripted world."""
        if request.request_id is not None:
            self.executed[request.request_id.root] += 1
        faulted = self._measurement(request) or self._job(request, core) or self._turn(request)
        return self._base(request, core) if faulted is None else faulted

    def _measurement(self, request: Request) -> Answer | None:
        if not isinstance(request, SubmitMeasurement):
            return None
        outcome = _nth(self._outcomes.measurements, self._submitted, "succeeded")
        self._submitted += 1
        if outcome == "infrastructure_failure":
            self._infrastructure.add(request.plan.model_dump_json())
        return {
            "workload_failure": Failed(MeasurementFailure.WORKLOAD),
            "lost": Unknown(),
        }.get(outcome)

    def _job(self, request: Request, core: CoreState) -> Answer | None:
        if not isinstance(request, ObserveOwnedJob):
            return None
        job = next(
            row
            for row in core.evaluation.jobs
            if isinstance(row, OwnedJob) and row.resource_id == request.resource_id
        )
        return Failed(accepted=True) if job.plan.model_dump_json() in self._infrastructure else None

    def _turn(self, request: Request) -> Answer | None:
        if not isinstance(request, DispatchTurn | ResumeSessionTurn):
            return None
        outcome = _nth(self._outcomes.turns, self._asked, "replies")
        self._asked += 1
        lost = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
        return {
            "unparsable": Succeeded(output_json="not json", resource_id=lost),
            "lost": Unknown(),
        }.get(outcome)


class KindFault:
    """A world that answers the requests of one kind a faulty way and the rest as the script."""

    def __init__(self, kind: str, how: str, base: SkeletonScript | None = None) -> None:
        """Fault ``kind``: once (``unknown_once``, ``retryable_once``) or every time (``silent``)."""
        self._base = base or SkeletonScript()
        self._kind = kind
        self._how = how
        self._seen = 0
        self.executed: Counter[str] = Counter()

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """The faulty answer for the chosen kind, else the scripted world's."""
        if request.request_id is not None:
            self.executed[request.request_id.root] += 1
        if request.kind == self._kind:
            position, self._seen = self._seen, self._seen + 1
            if self._how == "silent" or position == 0:
                return Retryable() if self._how == "retryable_once" else Unknown()
        return self._base(request, core)
