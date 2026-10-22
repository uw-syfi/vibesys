"""Native workspace effects through the existing workspace lifecycle owner."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from vs_core.api import (
    AdoptRevision,
    AttemptId,
    AttemptRef,
    DecisionId,
    DiscardWorkspace,
    EnsureWorkspace,
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
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.api.replay import ReplayArchive, ReplayWriterRecovery, proof_covers
from vs_decode.api import Unreadable
from vs_project.api import StoredEnvelope
from vs_proto.api import ReplayStore as ReplayRecords
from vs_proto.api import core_leaves, replay_agent, validate
from vs_proto.api import replay_workspace as pb
from vs_runtime._replay_evidence import adoption_evidence
from vs_runtime._replay_operations import ReplayOperationError, ReplayOperationHost
from vs_runtime._replay_owner import ReceiptReplayExecutor, ReplayOwnerCodec
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown
from vs_runtime._workspace_receipts import StoreWorkspaceReceipts
from vs_runtime._writer_reconciliation import WriterProofError
from vs_runtime.api.core_values import from_proto, to_proto

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_core.api import InvocationRef
    from vs_project.api import StoreFence
    from vs_proto.api import Span, replay
    from vs_runtime._core_requests import ExecutionResult, WorkspaceRequests, WorkspaceRoleRequest
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._writer_reconciliation import WriterRegistry


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
        writers: WriterRegistry | None = None,
    ) -> None:
        if not 0 < within_seconds < float("inf"):
            problem = "workspace replay within_seconds must be finite and positive"
            raise ReplayOperationError(problem)
        self._host = host
        self._writers = writers
        self._workspace_receipts = StoreWorkspaceReceipts(receipts)
        executor = ReceiptReplayExecutor(
            owner.execute,
            receipts,
            host,
            ReplayOwnerCodec(self._request, self._encode, lambda _: within_seconds, lambda _: None),
        )
        self._executor = executor

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        with self._root_access(span, fence):
            return await self._executor.execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        with self._root_access(span, fence):
            return await self._executor.inspect(span, fence)

    def _root_step(self, span: Span) -> bool:
        native = self._native(span)
        action = native.action
        if action is None:
            return False
        root_step = action.field == "adopt" or (
            action.field == "open" and action.value.mode is pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT
        )
        if native.attempt:
            binding = self._workspace_receipts.load_binding(
                AttemptRef(attempt_id=AttemptId(root=native.attempt), generation=native.generation)
            )
            root_step = root_step or (
                binding is not None and binding.mode is WorkspaceMode.EXCLUSIVE_ROOT
            )
        return root_step

    @contextmanager
    def _root_access(self, span: Span, fence: StoreFence) -> Iterator[None]:
        if not self._root_step(span):
            yield
            return
        if self._writers is None:
            problem = "Recovery blocked: previous writers may still modify this workspace; missing writer registry"
            raise WriterProofError(problem)
        with self._writers.reuse(run_id=self._host.run_id, fence=fence) as proof:
            self._guard_root(proof)
            yield

    def _guard_root(self, proof: replay.WriterTerminationProof) -> None:
        if proof.manifest is None or proof.manifest.backing is None:
            problem = "writer proof omitted backing identity"
            raise WriterProofError(problem)
        stored = self._host.store.load()
        if not isinstance(stored, StoredEnvelope):
            problem = "root reuse requires its durable native proof"
            raise WriterProofError(problem)
        request = ReplayWriterRecovery.from_records(
            ReplayRecords.from_binary(stored.payload).records
        ).require(proof.manifest.backing)
        self._cover_root_writers(ReplayRecords.from_binary(stored.payload), proof)
        if request.proof() != proof:
            problem = "durable root reuse proof differs from environment evidence"
            raise WriterProofError(problem)

    def _cover_root_writers(
        self, records: ReplayRecords, proof: replay.WriterTerminationProof
    ) -> None:
        """A previously empty proof cannot authorize reuse after a new writable grant."""
        for child in ReplayArchive.decode(records.to_binary()).spans:
            physical = child.replay_physical
            if (
                physical is None
                or physical.confirmed_absent
                or physical.has_field("termination_proof_digest")
            ):
                continue
            operation = child.replay
            if operation is None or operation.policy is None or operation.policy.request is None:
                continue
            payload = operation.policy.request
            if payload.type_url != replay_agent.ReplayAgentRequest.desc().type_name:
                proof_covers(child, proof, run_id=self._host.run_id)
                continue
            agent = replay_agent.ReplayAgentRequest.from_binary(payload.value)
            if agent.action is None or agent.action.field != "turn":
                continue
            turn = agent.action.value
            scope = None if turn.workspace is None else from_proto(turn.workspace)
            binding = None
            if isinstance(scope, Scope) and isinstance(scope.owner, AttemptId):
                binding = self._workspace_receipts.load_binding(
                    AttemptRef(attempt_id=scope.owner, generation=scope.generation)
                )
            if binding is None or binding.mode is WorkspaceMode.EXCLUSIVE_ROOT:
                proof_covers(child, proof, run_id=self._host.run_id)

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
            evidence = []
            for item in asked.evidence:
                decoded = adoption_evidence(item)
                if isinstance(decoded, Unreadable):
                    raise ReplayOperationError(str(decoded))
                evidence.append(decoded)
            return AdoptRevision(
                request_id=identity,
                scope=Scope(owner=RunId(root=self._host.run_id), generation=0),
                selection=selection,
                evidence=tuple(evidence),
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

    @classmethod
    async def _encode(
        cls, request: WorkspaceRoleRequest, result: ExecutionResult
    ) -> ReplayResult | ReplayUnknown:
        del request
        return cls._result(result)
