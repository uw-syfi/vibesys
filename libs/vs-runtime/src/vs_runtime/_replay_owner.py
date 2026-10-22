"""Receipt and authority mechanics shared by native effect owners."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import HostFence, HostId, RequestId, Until
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._effect_fence import EffectFences, Entered, MayHold
from vs_runtime._operation_requests import operation_fence
from vs_runtime._receipt_store import NeverBegun, ReceiptCorruptError
from vs_runtime._replay_operations import ReplayOperationError, _Lease
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_runtime._core_requests import ExecutionOutcome
    from vs_runtime._receipt_store import ExecutionHistory, ReceiptStore
    from vs_runtime._replay_operations import ReplayOperationHost


@dataclass(frozen=True)
class ReplayOwnerCodec[RequestT]:
    decode: Callable[[Span], RequestT]
    encode: Callable[[RequestT, ExecutionResult], Awaitable[ReplayResult | ReplayUnknown]]
    within: Callable[[Span], float]
    dispatch_check: Callable[[Span], None]


class ReceiptReplayExecutor[RequestT]:
    """Gate one physical call, recovering only through its owner's run-once path.

    Codecs translate values; the owner retains all resource lifecycle decisions.
    No positional driver or scheduler participates. Missing history becomes proof
    of absence only after closing the physical invocation gate.
    """

    def __init__(
        self,
        owner: Callable[[RequestT, ExecutionContext], Awaitable[ExecutionOutcome]],
        receipts: ReceiptStore,
        host: ReplayOperationHost,
        codec: ReplayOwnerCodec[RequestT],
    ) -> None:
        self._owner, self._receipts, self._host = owner, receipts, host
        self._request, self._encode = codec.decode, codec.encode
        self._within, self._dispatch_check = codec.within, codec.dispatch_check
        self._fences = EffectFences(receipts)

    def _context(self, span: Span, fence: StoreFence) -> ExecutionContext:
        if span.replay is None:
            problem = "owner replay requires immutable metadata"
            raise ReplayOperationError(problem)
        now = self._host.clock.now()
        return ExecutionContext(
            fence=HostFence(host_id=HostId(root=fence.host_id), epoch=fence.epoch),
            now_at=now,
            payload_digest=hashlib.sha256(span.replay.to_binary()).hexdigest(),
            until=Until(at=now + self._within(span)),
            lease=_Lease(self._host.store, fence, self._host.clock),
        )

    async def _execute(
        self, request: RequestT, context: ExecutionContext
    ) -> ReplayResult | ReplayUnknown:
        result = await self._owner(request, context)
        if not isinstance(result, ExecutionResult):
            return ReplayUnknown(result.detail)
        return await self._encode(request, result)

    def _history(self, span_id: str, context: ExecutionContext) -> ExecutionHistory:
        history = self._receipts.history(span_id)
        if not isinstance(history, NeverBegun) and history.payload_digest != context.payload_digest:
            problem = f"replay invocation payload conflict: {span_id}"
            raise ReplayOperationError(problem)
        return history

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        request = self._request(span)
        context = self._context(span, fence)
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            problem = "owner replay writer lease was lost"
            raise ReplayOperationError(problem)
        self._dispatch_check(span)
        identity = RequestId(root=span.id)
        key = operation_fence(identity)
        with self._receipts.exclusive():
            self._history(span.id, context)
            admission = self._fences.enter(key, by=identity, host=context.fence)
        if not isinstance(admission, Entered):
            problem = f"withdrawn replay invocation: {span.id}"
            raise ReplayOperationError(problem)
        try:
            result = await self._execute(request, context)
            if isinstance(result, ReplayUnknown):
                problem = f"owner replay outcome remains unknown: {result.reason}"
                raise ReplayOperationError(problem)
            return result
        finally:
            self._fences.leave(key, by=identity)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        request = self._request(span)
        context = self._context(span, fence)
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            return ReplayUnknown("owner replay writer lease was lost")
        try:
            with self._receipts.exclusive():
                history = self._history(span.id, context)
                closure = self._fences.close(
                    operation_fence(RequestId(root=span.id)),
                    by=RequestId(root=f"inspect/{fence.epoch}/{span.id}"),
                    host=context.fence,
                )
                if isinstance(closure, MayHold) and closure.live:
                    return ReplayPending()
                if isinstance(history, NeverBegun):
                    # The outer gate is closed, and an earlier writer cannot pass
                    # the owner's lease check to install a begun marker later.
                    return ReplayAbsent()
                sealed = self._receipts.sealed(span.id, ExecutionResult)
            if sealed is not None:
                return await self._encode(request, sealed[1])
            return await self._execute(request, context)
        except ReceiptCorruptError as error:
            return ReplayUnknown(str(error))
