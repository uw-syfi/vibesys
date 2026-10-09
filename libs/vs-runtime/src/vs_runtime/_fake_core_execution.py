"""Faithful identity/fence Fake for the runtime request translation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import (
    ContractError,
    EventId,
    Observation,
    ObservationStatus,
    Request,
    RequestId,
    RequestObserved,
)
from vs_runtime._core_record import PublicationAcknowledgement, append_publication
from vs_runtime._core_requests import (
    REQUEST_DISPATCH,
    ExecutionContext,
    ExecutionResult,
    ExecutorRole,
)

if TYPE_CHECKING:
    from vs_project.api import StateStore
    from vs_runtime._core_record import Publication, PublicationContext


@dataclass(frozen=True)
class ExecutedRequest:
    """One accepted physical fake execution and its immutable correlated outcome."""

    request: Request
    context: ExecutionContext
    result: ExecutionResult


class FakeRequestExecution:
    """Canonical identity deduplication, payload conflicts and monotone host epochs.

    No missing acceptance, child manifest or termination fact becomes success.
    This Fake records an external execution once and returns explicit Unknown;
    owning-lane Fakes provide operation-specific terminal facts later. Repeated
    calls return the original observation and never perform a second execution.
    """

    def __init__(self, role: ExecutorRole) -> None:
        self._role = role
        self._executions: dict[RequestId, ExecutedRequest] = {}
        self._epochs: dict[str, int] = {}

    @property
    def executions(self) -> tuple[ExecutedRequest, ...]:
        """Immutable accepted physical executions, not merely calls."""
        return tuple(self._executions.values())

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        """Honor the same role, identity, payload and fence contract as translators."""
        if request.request_id is None:
            raise ContractError(("request_id",), "canonical identity required")
        if REQUEST_DISPATCH[type(request)] != self._role:
            raise ContractError(("role",), "request routed to another owning role")
        owner = f"{request.scope.owner.kind}:{request.scope.owner.root}:{request.scope.generation}"
        if context.fence.epoch < self._epochs.get(owner, 0):
            raise ContractError(("fence", "epoch"), "stale execution host")
        prior = self._executions.get(request.request_id)
        if prior is not None:
            if prior.request != request or prior.context.payload_digest != context.payload_digest:
                raise ContractError(("request_id",), "same identity with another payload")
            self._epochs[owner] = context.fence.epoch
            return prior.result
        self._epochs[owner] = context.fence.epoch
        result = ExecutionResult(
            observation=RequestObserved(
                observation=Observation(
                    event_id=EventId(root=f"{request.request_id.root}:observation:0"),
                    request_id=request.request_id,
                    scope=request.scope,
                    admission_id=request.admission_id,
                    sequence=0,
                    observed_at=context.now_at,
                    status=ObservationStatus.UNKNOWN,
                    diagnostic="fake external execution awaiting authoritative observation",
                )
            )
        )
        self._executions[request.request_id] = ExecutedRequest(request, context, result)
        return result


class FakePublicationDelivery:
    """In-memory durable publication ledger with exact stable-ID conflict checks."""

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._publications: tuple[Publication, ...] = ()

    def read(self) -> tuple[Publication, ...]:
        """Return the acknowledged publication history."""
        return self._publications

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        """Honor the host fence, then apply the shared strict journal contract."""
        if not self._store.verify(context.fence, now=context.now_at):
            raise ContractError(("fence",), "publication host no longer owns the run")
        self._publications = append_publication(self._publications, publication)
        return PublicationAcknowledgement(
            publication_id=publication.publication_id, sequence=publication.sequence
        )
