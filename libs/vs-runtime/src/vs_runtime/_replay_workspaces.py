"""Native workspace effects through the existing workspace lifecycle owner."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from vs_core.api import (
    AdoptRevision,
    AttemptId,
    AttemptRef,
    DecisionId,
    DiscardWorkspace,
    EnsureWorkspace,
    HostFence,
    HostId,
    ObservationStatus,
    RequestId,
    RetainedCandidate,
    RetainRevision,
    RevisionRef,
    RunId,
    Scope,
    SettlementId,
    SnapshotAndRetain,
    TrustedBaseline,
    Until,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_proto.api import core_leaves, validate
from vs_proto.api import replay_workspace as pb
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._effect_fence import EffectFences, Entered, MayHold
from vs_runtime._operation_requests import operation_fence
from vs_runtime._receipt_store import NeverBegun, ReceiptCorruptError
from vs_runtime._replay_operations import ReplayOperationError, ReplayOperationHost, _Lease
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown
from vs_runtime.api.core_values import from_proto, to_proto

if TYPE_CHECKING:
    from vs_core.api import EvidenceRef, InvocationRef
    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_runtime._core_requests import WorkspaceRequests, WorkspaceRoleRequest
    from vs_runtime._receipt_store import ExecutionHistory, ReceiptStore


_RETENTION: dict[pb.ReplayRetention, Literal["wip", "candidate"]] = {
    pb.ReplayRetention.WIP: "wip",
    pb.ReplayRetention.CANDIDATE: "candidate",
}
_STATUS = {
    ObservationStatus.SUCCEEDED: pb.ReplayWorkspaceStatus.SUCCEEDED,
    ObservationStatus.FAILED: pb.ReplayWorkspaceStatus.FAILED,
    ObservationStatus.REJECTED: pb.ReplayWorkspaceStatus.REJECTED,
    ObservationStatus.CANCELLED: pb.ReplayWorkspaceStatus.CANCELLED,
}


def _revision(message: core_leaves.RevisionRef | None) -> RevisionRef:
    if message is None:
        problem = "workspace replay requires a revision"
        raise ReplayOperationError(problem)
    result = from_proto(message)
    if not isinstance(result, RevisionRef):
        problem = "workspace replay revision has another type"
        raise ReplayOperationError(problem)
    return result


class _Common(TypedDict):
    request_id: RequestId
    scope: Scope
    admission_id: DecisionId
    attempt: AttemptRef


class WorkspaceReplayExecutor:
    """Retain, settle and adopt through the same owner used by production runs.

    Workspace acquisition and snapshots also use that owner, including its
    durable bindings, root holds and positive handle reattachment. Recovery of a
    begun call uses the owner's existing run-once recovery path. Unknown work
    remains unfinished; it never becomes a failed round or proof of absence.
    """

    def __init__(
        self,
        owner: WorkspaceRequests,
        receipts: ReceiptStore,
        host: ReplayOperationHost,
        *,
        within_seconds: float,
    ) -> None:
        if not 0 < within_seconds < float("inf"):
            problem = "workspace replay within_seconds must be finite and positive"
            raise ReplayOperationError(problem)
        self._owner, self._receipts, self._host = owner, receipts, host
        self._within = within_seconds
        self._fences = EffectFences(receipts)

    @staticmethod
    def _native(span: Span) -> pb.ReplayWorkspaceRequest:
        validate(span)
        operation = span.replay
        if operation is None or operation.policy is None:
            problem = "workspace replay requires immutable request metadata"
            raise ReplayOperationError(problem)
        payload = operation.policy.request
        if payload is None or payload.type_url != pb.ReplayWorkspaceRequest.desc().type_name:
            problem = "workspace replay request type differs"
            raise ReplayOperationError(problem)
        if operation.policy.reply_type != pb.ReplayWorkspaceResult.desc().type_name:
            problem = "workspace replay reply type differs"
            raise ReplayOperationError(problem)
        native = pb.ReplayWorkspaceRequest.from_binary(payload.value)
        validate(native)
        if native.to_binary() != native.to_binary(write_unknown_fields=False):
            problem = "workspace replay request has unknown fields"
            raise ReplayOperationError(problem)
        return native

    def _request(self, span: Span) -> WorkspaceRoleRequest:
        native = self._native(span)
        identity = RequestId(root=span.id)
        if native.action is not None and native.action.field == "adopt":
            asked = native.action.value
            revision = _revision(asked.revision)
            selection = (
                TrustedBaseline(revision=revision)
                if asked.baseline
                else RetainedCandidate(
                    settlement_id=SettlementId(root=asked.settlement_id), revision=revision
                )
            )
            return AdoptRevision(
                request_id=identity,
                scope=Scope(owner=RunId(root=self._host.run_id), generation=0),
                selection=selection,
                evidence=tuple(cast("EvidenceRef", from_proto(item)) for item in asked.evidence),
            )
        attempt = AttemptRef(
            attempt_id=AttemptId(root=native.attempt), generation=native.generation
        )
        common: _Common = {
            "request_id": identity,
            "scope": Scope(owner=attempt.attempt_id, generation=attempt.generation),
            "admission_id": DecisionId(root=native.admission),
            "attempt": attempt,
        }
        match native.action:
            case None:
                problem = "workspace replay requires an action"
                raise ReplayOperationError(problem)
            case action if action.field == "open":
                return EnsureWorkspace(
                    **common,
                    plan=WorkspacePlan(
                        mode={
                            pb.ReplayWorkspaceMode.ISOLATED: WorkspaceMode.ISOLATED_CHILD,
                            pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT: WorkspaceMode.EXCLUSIVE_ROOT,
                        }[action.value.mode],
                        base=_revision(action.value.base),
                    ),
                )
            case action if action.field == "snapshot":
                return SnapshotAndRetain(
                    **common,
                    retention=_RETENTION[action.value.retention],
                    invocation=None
                    if action.value.invocation is None
                    else cast("InvocationRef", from_proto(action.value.invocation)),
                )
            case action if action.field == "retain":
                return RetainRevision(
                    **common,
                    retention=_RETENTION[action.value.retention],
                    revision=_revision(action.value.revision),
                )
            case action if action.field == "settle":
                return DiscardWorkspace(**common)
            case _:
                problem = "workspace replay action is not supported"
                raise ReplayOperationError(problem)

    def _context(self, span: Span, fence: StoreFence) -> ExecutionContext:
        if span.replay is None:
            problem = "workspace replay requires immutable metadata"
            raise ReplayOperationError(problem)
        now = self._host.clock.now()
        return ExecutionContext(
            fence=HostFence(host_id=HostId(root=fence.host_id), epoch=fence.epoch),
            now_at=now,
            payload_digest=hashlib.sha256(span.replay.to_binary()).hexdigest(),
            until=Until(at=now + self._within),
            lease=_Lease(self._host.store, fence, self._host.clock),
        )

    @staticmethod
    def _result(result: ExecutionResult) -> ReplayResult | ReplayUnknown:
        observed = result.observation
        seen = observed.observation
        if not seen.terminal or seen.status is ObservationStatus.UNKNOWN:
            return ReplayUnknown(seen.diagnostic)
        value = pb.ReplayWorkspaceResult(
            status=_STATUS[seen.status],
            revision=None
            if observed.revision is None
            else cast("core_leaves.RevisionRef", to_proto(observed.revision)),
            resource_id=None if seen.resource_id is None else seen.resource_id.root,
            released=seen.released,
            detail=seen.diagnostic,
        )
        validate(value)
        return ReplayResult(value)

    async def _execute(
        self, request: WorkspaceRoleRequest, context: ExecutionContext
    ) -> ReplayResult | ReplayUnknown:
        result = await self._owner.execute(request, context)
        if not isinstance(result, ExecutionResult):
            return ReplayUnknown(result.detail)
        return self._result(result)

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
            problem = "workspace replay writer lease was lost"
            raise ReplayOperationError(problem)
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
                problem = f"workspace replay outcome remains unknown: {result.reason}"
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
            return ReplayUnknown("workspace replay writer lease was lost")
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
                return self._result(sealed[1])
            return await self._execute(request, context)
        except ReceiptCorruptError as error:
            return ReplayUnknown(str(error))
