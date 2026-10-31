"""The jobs a scope owns, registered once by whoever submits them.

Core names an evaluation job by its resource id and asks about it later: to collect its evidence,
to poll it, to cancel it, and to close the attempt scope that holds it. The evaluation executor
answers only for a job it finds here, under the handle the resource id names, and closing a scope
lists exactly the handles indexed under it. So every submitter writes the same two records through
this registry before it contacts the executor: the job's ``JobRecord`` and the scope's index. The
polling executor of ``MeasurementRequests`` does, and so does a step runner's ``JobStepRun``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import DecisionId, MeasurementPlan, RequestId, Scope

if TYPE_CHECKING:
    from vs_core.api import SubmitMeasurement
    from vs_runtime._receipt_store import ReceiptStore

_JOBS = "evaluation-jobs"
_SCOPES = "evaluation-scopes"


class JobRecord(BaseModel):
    """A registered job: the intent receipt written before the effect."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    handle_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    scope: Scope
    admission_id: DecisionId | None = None
    plan: MeasurementPlan
    reuses: RequestId | None = None
    """The submission whose ended job this one reads, when it reuses an earlier run."""
    parked: bool = False
    relaunch: int = Field(default=0, ge=0)

    @classmethod
    def of(
        cls, request: SubmitMeasurement, request_id: RequestId, handle: str, payload_digest: str
    ) -> JobRecord:
        """The record of the job *request* submits under *handle*."""
        return cls(
            handle_id=handle,
            request_id=request_id.root,
            payload_digest=payload_digest,
            scope=request.scope,
            admission_id=request.admission_id,
            plan=request.plan,
            reuses=request.reuses,
        )

    @property
    def executor_handle(self) -> str:
        """The executor job this record's observations read."""
        return (
            self.handle_id if self.relaunch == 0 else f"{self.handle_id}-relaunch-{self.relaunch}"
        )


class ScopeJobs(BaseModel):
    """The jobs registered under one admission episode of one scope.

    Whether the episode closed is its effect fence's to say (``scope_fence``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scope: Scope
    admission_id: DecisionId | None = None
    handles: tuple[str, ...] = ()


def scope_key(scope: Scope, admission_id: DecisionId | None) -> str:
    """The name of one admission episode of one scope."""
    episode = "" if admission_id is None else admission_id.root
    return f"{scope.owner.kind}:{scope.owner.root}:{scope.generation}:{episode}"


class JobRegistry:
    """The records of the jobs submitted under one run's receipt store."""

    def __init__(self, store: ReceiptStore) -> None:
        """Keep the records in *store*, the one its evaluation executor reads."""
        self._store = store

    def record(self, handle_id: str) -> JobRecord | None:
        """The job registered under *handle_id*, or ``None``."""
        return self._store.load(_JOBS, "job", handle_id, JobRecord)

    def handles(self, scope: Scope, admission_id: DecisionId | None) -> tuple[str, ...]:
        """The handles indexed under one admission episode of *scope*, in registration order."""
        index = self._store.load(_SCOPES, "scope", scope_key(scope, admission_id), ScopeJobs)
        return () if index is None else index.handles

    def register(self, record: JobRecord) -> None:
        """Index the job under its scope and record it. Registering the same job again is a no-op."""
        with self._store.exclusive():
            self._index(record)
            self._store.record_once(_JOBS, "job", record.handle_id, record)

    def replace(self, record: JobRecord) -> None:
        """Overwrite a registered job's record: it parks, or relaunches under a new handle."""
        self._store.replace(_JOBS, "job", record.handle_id, record)

    def unindex(self, scope: Scope, admission_id: DecisionId | None, handle: str) -> None:
        """Drop *handle* from its scope's index: its submission was refused, so it never was a job."""

        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, None]:
            if stored is None or handle not in stored.handles:
                return None, None
            kept = tuple(item for item in stored.handles if item != handle)
            return stored.model_copy(update={"handles": kept}), None

        self._store.modify(_SCOPES, "scope", scope_key(scope, admission_id), ScopeJobs, decide)

    def _index(self, record: JobRecord) -> None:
        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, None]:
            index = stored or ScopeJobs(scope=record.scope, admission_id=record.admission_id)
            if record.handle_id in index.handles:
                return None, None
            return index.model_copy(update={"handles": (*index.handles, record.handle_id)}), None

        key = scope_key(record.scope, record.admission_id)
        self._store.modify(_SCOPES, "scope", key, ScopeJobs, decide)
