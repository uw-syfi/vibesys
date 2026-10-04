"""Owned evaluation observations, independent of agent polling turns."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_evaluation.agent_evidence import EvidenceFingerprints
from vs_evaluation.agent_models import (
    EVALUATION_ACCESS_STATE_PATH,
    MAX_AGENT_AWAIT_S,
    EvaluationAgentState,
    HandleAccess,
    SubmittedSemanticEvaluation,
)
from vs_evaluation.coordinator import EvaluationLifecycleError
from vs_evaluation.models import (
    EvaluationAwaitResult,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationState,
    EvaluationTimedOut,
    StageState,
    StoredEvaluation,
)
from vs_evaluation.scope_state import ScopeLifecycleStore

if TYPE_CHECKING:
    from vs_evaluation.state_namespace import EvaluationStateNamespace


class OwnedEvaluationDependencies(BaseModel):
    """Host-authorized nonempty, unique handles in one scope generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    handles: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_handles(self) -> OwnedEvaluationDependencies:
        """Reject empty identities and duplicated dependencies."""
        if any(not handle or handle.strip() != handle for handle in self.handles):
            message = "handles must contain nonempty trimmed identities"
            raise ValueError(message)
        if len(self.handles) != len(set(self.handles)):
            message = "handles must be unique"
            raise ValueError(message)
        return self


TerminalEvaluationResult = Annotated[
    EvaluationCompleted | EvaluationFailed | EvaluationCanceled, Field(discriminator="outcome")
]


class EvaluationPending(BaseModel):
    """Durable work still has no terminal conclusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["pending"] = "pending"
    state: Literal[EvaluationState.QUEUED, EvaluationState.STARTING, EvaluationState.RUNNING]


class EvaluationUnknown(BaseModel):
    """An observation failed without proving a terminal conclusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["unknown"] = "unknown"
    detail: str = Field(min_length=1)


EvaluationSettlementOutcome = Annotated[
    TerminalEvaluationResult | EvaluationPending | EvaluationUnknown, Field(discriminator="outcome")
]


class EvaluationSettlementObservation(BaseModel):
    """Trusted immutable identity and one revisioned lifecycle observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handle_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    fingerprints: EvidenceFingerprints
    revision: int | None = Field(ge=0)
    result: EvaluationSettlementOutcome


class SettlementErrorCode(StrEnum):
    """Stable dependency rejection categories."""

    UNOWNED = "unowned"
    STALE_GENERATION = "stale_generation"
    UNKNOWN_HANDLE = "unknown_handle"
    IDENTITY_CONFLICT = "identity_conflict"


class EvaluationDependencyError(ValueError):
    """A dependency cannot be attributed to the requested owner."""

    def __init__(self, code: SettlementErrorCode, handle_id: str) -> None:
        """Retain the typed reason and offending handle."""
        self.code = code
        self.handle_id = handle_id
        super().__init__(f"evaluation dependency {handle_id!r}: {code.value}")


class EvaluationSettlements(Protocol):
    """Observe owned dependencies without changing job or ownership lifetime.

    Invalid ownership raises EvaluationDependencyError. Unknown observations
    never imply terminality. wait_any renews bounded host waits internally;
    cancelling it only cancels observers, never jobs or durable ownership.
    """

    async def observe(
        self, dependencies: OwnedEvaluationDependencies
    ) -> tuple[EvaluationSettlementObservation, ...]:
        """Validate identities and read each durable state before external observation."""
        ...

    async def wait_any(
        self, dependencies: OwnedEvaluationDependencies
    ) -> tuple[EvaluationSettlementObservation, ...]:
        """Return settled dependencies, or Unknown when observation is ambiguous."""
        ...


def recorded_result(record: StoredEvaluation) -> EvaluationSettlementOutcome:
    """Project durable facts without inferring successful execution."""
    match record.state:
        case EvaluationState.SUCCEEDED:
            if tuple(stage.name for stage in record.stage_results) != tuple(
                stage.name for stage in record.request.stages
            ) or any(stage.state is not StageState.SUCCEEDED for stage in record.stage_results):
                return EvaluationUnknown(
                    detail="successful record lacks complete successful stage evidence"
                )
            return EvaluationCompleted(handle_id=record.handle_id, stages=record.stage_results)
        case EvaluationState.FAILED:
            if not record.failure:
                return EvaluationUnknown(detail="failed record has no failure evidence")
            return EvaluationFailed(handle_id=record.handle_id, message=record.failure)
        case EvaluationState.CANCELED | EvaluationState.SUPERSEDED:
            return EvaluationCanceled(handle_id=record.handle_id, state=record.state)
        case EvaluationState.QUEUED | EvaluationState.STARTING | EvaluationState.RUNNING:
            return EvaluationPending(state=record.state)


class EvaluationSettlementBackend(Protocol):
    """Durable ownership and cancellation-safe bounded observation mechanism."""

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Read claimed identities in the scope."""
        ...

    async def recorded_snapshot(self, handle_id: str) -> StoredEvaluation:
        """Read durable state without inspecting external work."""
        ...

    async def recorded_submission(self, handle_id: str) -> SubmittedSemanticEvaluation | None:
        """Read authoritative submitted identity; None means legacy identity is unavailable."""
        ...

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        """Refresh and await a result without cancelling jobs on caller cancellation."""
        ...


class ServiceEvaluationSettlements:
    """Read service ownership and durable records, then use bounded host waits."""

    def __init__(
        self, backend: EvaluationSettlementBackend, namespace: EvaluationStateNamespace
    ) -> None:
        """Bind the same backend and ownership namespace as EvaluationAgentService."""
        self._backend = backend
        self._namespace = namespace

    async def observe(
        self, dependencies: OwnedEvaluationDependencies
    ) -> tuple[EvaluationSettlementObservation, ...]:
        """Validate every dependency before observing any external job."""
        access = self._namespace.load_optional(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
        accesses = {item.handle_id: item for item in access.handles} if access else {}
        self._validate_generation(dependencies)
        for handle in dependencies.handles:
            if handle not in accesses:
                raise EvaluationDependencyError(SettlementErrorCode.UNKNOWN_HANDLE, handle)
            if accesses[handle].scope_id != dependencies.scope_id:
                raise EvaluationDependencyError(SettlementErrorCode.UNOWNED, handle)
        try:
            owned = await self._backend.owned_handles(dependencies.scope_id)
        except (TimeoutError, OSError) as error:
            self._validate_generation(dependencies)
            return tuple(
                EvaluationSettlementObservation(
                    handle_id=handle,
                    scope_id=dependencies.scope_id,
                    generation=dependencies.generation,
                    fingerprints=accesses[handle].fingerprints,
                    revision=None,
                    result=EvaluationUnknown(detail=str(error) or type(error).__name__),
                )
                for handle in dependencies.handles
            )
        observations = []
        for handle in dependencies.handles:
            if handle not in owned:
                raise EvaluationDependencyError(SettlementErrorCode.UNOWNED, handle)
            observations.append(await self._observe_one(dependencies, accesses[handle]))
        self._validate_generation(dependencies)
        return tuple(observations)

    def _validate_generation(self, dependencies: OwnedEvaluationDependencies) -> None:
        scope = next(
            (
                item
                for item in ScopeLifecycleStore(self._namespace).snapshot().scopes
                if item.scope_id == dependencies.scope_id
            ),
            None,
        )
        current_generation = scope.generation if scope else 0
        if dependencies.generation != current_generation:
            raise EvaluationDependencyError(
                SettlementErrorCode.STALE_GENERATION, dependencies.handles[0]
            )

    async def _observe_one(
        self, dependencies: OwnedEvaluationDependencies, access: HandleAccess
    ) -> EvaluationSettlementObservation:
        handle = access.handle_id
        try:
            record = await self._backend.recorded_snapshot(handle)
            submitted = await self._backend.recorded_submission(handle)
        except EvaluationLifecycleError as error:
            raise EvaluationDependencyError(SettlementErrorCode.UNKNOWN_HANDLE, handle) from error
        except (TimeoutError, OSError) as error:
            return EvaluationSettlementObservation(
                handle_id=handle,
                scope_id=dependencies.scope_id,
                generation=dependencies.generation,
                fingerprints=access.fingerprints,
                revision=None,
                result=EvaluationUnknown(detail=str(error) or type(error).__name__),
            )
        if record.request.owner_scope != dependencies.scope_id:
            raise EvaluationDependencyError(SettlementErrorCode.UNOWNED, handle)
        if record.request.owner_generation != dependencies.generation:
            raise EvaluationDependencyError(SettlementErrorCode.STALE_GENERATION, handle)
        if submitted is not None and (
            submitted.handle_id != handle or submitted.fingerprints != access.fingerprints
        ):
            raise EvaluationDependencyError(SettlementErrorCode.IDENTITY_CONFLICT, handle)
        return EvaluationSettlementObservation(
            handle_id=handle,
            scope_id=dependencies.scope_id,
            generation=dependencies.generation,
            fingerprints=access.fingerprints,
            revision=record.revision,
            result=recorded_result(record)
            if submitted is not None
            else EvaluationUnknown(detail="durable submitted identity is unavailable"),
        )

    async def wait_any(
        self, dependencies: OwnedEvaluationDependencies
    ) -> tuple[EvaluationSettlementObservation, ...]:
        """Prefer durable terminal records, then renew cancellation-safe host waits."""
        while True:
            observations = await self.observe(dependencies)
            settled = tuple(
                item for item in observations if not isinstance(item.result, EvaluationPending)
            )
            if settled:
                return settled
            tasks = [asyncio.create_task(self._wait_one(item)) for item in observations]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                results = tuple(task.result() for task in tasks if task in done)
                terminal = tuple(
                    item for item in results if not isinstance(item.result, EvaluationPending)
                )
                if terminal:
                    return terminal
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_one(
        self, observation: EvaluationSettlementObservation
    ) -> EvaluationSettlementObservation:
        try:
            result = await self._backend.await_result(observation.handle_id, MAX_AGENT_AWAIT_S)
        except (TimeoutError, OSError, EvaluationLifecycleError) as error:
            return observation.model_copy(
                update={"result": EvaluationUnknown(detail=str(error) or type(error).__name__)}
            )
        if isinstance(result, EvaluationTimedOut):
            return observation
        refreshed = await self.observe(
            OwnedEvaluationDependencies(
                scope_id=observation.scope_id,
                generation=observation.generation,
                handles=(observation.handle_id,),
            )
        )
        if refreshed[0].fingerprints != observation.fingerprints:
            raise EvaluationDependencyError(
                SettlementErrorCode.IDENTITY_CONFLICT, observation.handle_id
            )
        return refreshed[0]


__all__ = [
    "EvaluationDependencyError",
    "EvaluationPending",
    "EvaluationSettlementBackend",
    "EvaluationSettlementObservation",
    "EvaluationSettlementOutcome",
    "EvaluationSettlements",
    "EvaluationUnknown",
    "OwnedEvaluationDependencies",
    "ServiceEvaluationSettlements",
    "SettlementErrorCode",
    "TerminalEvaluationResult",
]
