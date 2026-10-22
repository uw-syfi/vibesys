"""Typed keyed replay dispatch through the existing registered-operation owners.

This binding calls the owning executors directly. It does not instantiate a
positional flow driver or replay a scheduler instruction stream. Their receipts
remain the authority for physical execution and external-effect inspection.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import (
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InspectRequest,
    ObservationStatus,
    OperationId,
    ReissueProof,
    RequestId,
    RunId,
    Scope,
    Until,
)
from vs_proto.api import validate
from vs_runtime._core_requests import ExecutionContext
from vs_runtime._effect_fence import EffectFences, NothingHeld
from vs_runtime._observation_factory import ObservationFactory
from vs_runtime._operation_receipts import NamespaceOperationReceipts
from vs_runtime._operation_requests import RegisteredOperationRequests, operation_fence
from vs_runtime._replay_runtime import ReplayAbsent, ReplayResult, ReplayUnknown

if TYPE_CHECKING:
    from collections.abc import Callable

    from protobuf import Message
    from pydantic import BaseModel

    from vs_core.api import OperationRequest, RequestObserved, TargetObservation
    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_runtime._operation_catalog import OperationCatalog
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._replay_store import ReplayStore
    from vs_sim.api import Clock


class ReplayOperationError(ValueError):
    """A native request cannot be served by its declared owning operation."""


@dataclass(frozen=True)
class ReplayOperationBinding:
    """An exact native request/reply pair and its existing owner's value codec.

    ``operation`` is the semantic operation name. ``kind`` is the registered
    owner kind. Codecs only translate values; execution and recovery always go
    through the catalog's owner and its shared receipt machinery.
    """

    operation: str
    kind: str
    request: type[Message]
    reply: type[Message]
    decode: Callable[[Message], OperationRequest]
    encode: Callable[[BaseModel], Message]
    within_seconds: float
    paid: bool
    dispatch_check: Callable[[Message], None] | None = None

    def __post_init__(self) -> None:
        if not self.operation or not self.kind:
            problem = "replay operation and owner kind must be nonempty"
            raise ReplayOperationError(problem)
        if not 0 < self.within_seconds < float("inf"):
            problem = "replay operation within_seconds must be finite and positive"
            raise ReplayOperationError(problem)


@dataclass(frozen=True)
class _Lease:
    store: ReplayStore
    fence: StoreFence
    clock: Clock

    def verify(self, *, now_at: float) -> bool:
        del now_at
        return self.store.verify(self.fence, self.clock.now())

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        # The replay writer owns renewal. An effect must not extend its authority
        # independently of the writer that records the resulting observation.
        del now_at, lease_duration
        if not self.verify(now_at=self.clock.now()):
            problem = "replay operation writer lease was lost"
            raise ReplayOperationError(problem)


@dataclass(frozen=True)
class ReplayOperationHost:
    """The current run identity, writer authority and injected execution clock."""

    run_id: str
    store: ReplayStore
    clock: Clock


class RegisteredReplayExecutor:
    """Bind exact typed replay requests to real owners, sharing their durable receipts.

    A missing receipt proves absence only through the existing fenced inspector.
    Unfinished effects are inspected by their owner; uncertainty never turns into
    a retry. Construct a fresh executor on Resume with freshly bound resources
    and the same run namespace, catalog and codecs.
    """

    def __init__(
        self,
        catalog: OperationCatalog,
        receipts: ReceiptStore,
        bindings: tuple[ReplayOperationBinding, ...],
        *,
        host: ReplayOperationHost,
    ) -> None:
        self._catalog = catalog
        self._fences = EffectFences(receipts)
        self._receipts = NamespaceOperationReceipts(receipts)
        self._operations = RegisteredOperationRequests(
            catalog, self._receipts, ObservationFactory(receipts)
        )
        self._store, self._clock = host.store, host.clock
        self._scope = Scope(owner=RunId(root=host.run_id), generation=0)
        self._bindings = {binding.operation: binding for binding in bindings}
        if len(self._bindings) != len(bindings):
            problem = "duplicate replay operation binding"
            raise ReplayOperationError(problem)
        entries = {entry.registration.descriptor.kind: entry for entry in catalog.entries}
        for binding in bindings:
            entry = entries.get(binding.kind)
            if entry is None or entry.owner is None:
                problem = f"replay operation {binding.operation}: owner is absent"
                raise ReplayOperationError(problem)

    def _request(
        self, span: Span
    ) -> tuple[ReplayOperationBinding, ExecuteRegisteredOperation, Message]:
        validate(span)
        operation = span.replay
        if operation is None or operation.key is None or operation.policy is None:
            problem = "replay operation requires immutable typed metadata"
            raise ReplayOperationError(problem)
        binding = self._bindings.get(operation.key.operation)
        if binding is None:
            problem = f"unbound replay operation: {operation.key.operation}"
            raise ReplayOperationError(problem)
        policy = operation.policy
        payload = policy.request
        if payload is None or payload.type_url != binding.request.desc().type_name:
            problem = f"replay operation {binding.operation}: request type differs"
            raise ReplayOperationError(problem)
        if policy.reply_type != binding.reply.desc().type_name:
            problem = f"replay operation {binding.operation}: reply type differs"
            raise ReplayOperationError(problem)
        native = binding.request.from_binary(payload.value)
        validate(native)
        if native.to_binary() != native.to_binary(write_unknown_fields=False):
            problem = f"replay operation {binding.operation}: request has unknown fields"
            raise ReplayOperationError(problem)
        decoded = binding.decode(native)
        wire = self._catalog.registry.encode(decoded)
        if wire.schema_ref.kind != binding.kind:
            problem = f"replay operation {binding.operation}: codec names another owner"
            raise ReplayOperationError(problem)
        request = ExecuteRegisteredOperation(
            request_id=RequestId(root=span.id),
            scope=self._scope,
            operation_id=OperationId(root=span.id),
            operation=wire,
            retry_limit=0,
        )
        return binding, request, native

    def _context(
        self, span: Span, fence: StoreFence, binding: ReplayOperationBinding
    ) -> ExecutionContext:
        now = self._clock.now()
        if span.replay is None:
            problem = "replay operation requires immutable metadata"
            raise ReplayOperationError(problem)
        return ExecutionContext(
            fence=HostFence(host_id=HostId(root=fence.host_id), epoch=fence.epoch),
            now_at=now,
            payload_digest=hashlib.sha256(span.replay.to_binary()).hexdigest(),
            until=Until(at=now + binding.within_seconds),
            lease=_Lease(self._store, fence, self._clock),
        )

    @staticmethod
    def _result(
        binding: ReplayOperationBinding, observed: RequestObserved | TargetObservation
    ) -> ReplayResult:
        seen = observed.observation
        if seen.status is not ObservationStatus.SUCCEEDED or observed.outcome is None:
            problem = (
                f"replay operation {binding.operation}: owner has no conclusive typed outcome: "
                f"{seen.status.value}: {seen.diagnostic}"
            )
            raise ReplayOperationError(problem)
        reply = binding.encode(observed.outcome)
        if type(reply) is not binding.reply:
            problem = f"replay operation {binding.operation}: outcome type differs"
            raise ReplayOperationError(problem)
        validate(reply)
        return ReplayResult(reply, paid=binding.paid)

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        binding, request, native = self._request(span)
        if binding.dispatch_check is not None:
            binding.dispatch_check(native)
        result = await self._operations.execute(request, self._context(span, fence, binding))
        return self._result(binding, result.observation)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayUnknown:
        binding, _, _ = self._request(span)
        context = self._context(span, fence, binding)
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            return ReplayUnknown("replay operation writer lease was lost")
        with self._receipts.store.exclusive():
            record = self._receipts.record(span.id)
            if record is None:
                closure = self._fences.close(
                    operation_fence(RequestId(root=span.id)),
                    by=RequestId(root=f"inspect/{fence.epoch}/{span.id}"),
                    host=context.fence,
                )
                if not isinstance(closure, NothingHeld):
                    return ReplayUnknown("an operation delivery may still be creating its receipt")
        if record is not None and record.payload_digest != context.payload_digest:
            problem = f"replay invocation payload conflict: {span.id}"
            raise ReplayOperationError(problem)
        request = InspectRequest(
            request_id=RequestId(root=f"inspect/{fence.epoch}/{span.id}"),
            scope=self._scope,
            target=RequestId(root=span.id),
        )
        result = await self._operations.execute(request, self._context(span, fence, binding))
        observed = result.observation.target
        if observed is None:
            return ReplayUnknown("owner inspection returned no target facts")
        seen = observed.observation
        if observed.reissue is ReissueProof.NEVER_BEGAN or (
            seen.status is ObservationStatus.REJECTED
            and seen.terminal
            and not seen.accepted
            and seen.released
            and seen.children_complete
        ):
            return ReplayAbsent()
        if observed.observation.status is ObservationStatus.UNKNOWN:
            return ReplayUnknown(observed.observation.diagnostic)
        return self._result(binding, observed)
