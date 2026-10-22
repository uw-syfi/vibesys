"""Durable local evaluation evidence and prerequisites for inherited workspace use."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, cast

from vs_decode.api import Unreadable, decode_json
from vs_evaluation.api import (
    ExecutorObservation,
    ExecutorRejectedError,
    evaluation_work_digest,
    join_observation,
)
from vs_project.api import ProjectStateError
from vs_proto.api import evaluation_receipts as pb
from vs_proto.api import from_mapping, to_mapping, validate

if TYPE_CHECKING:
    from vs_evaluation.api import EvaluationRequest, EvaluationStateNamespace


class EvaluationReceiptUnconfirmedError(RuntimeError):
    """An evaluation receipt write was unconfirmed; stop before further effects."""


def _write_receipt(namespace: EvaluationStateNamespace, path: str, contents: bytes) -> None:
    try:
        namespace.write_bytes(path, contents)
    except (OSError, ProjectStateError) as error:
        problem = f"evaluation receipt write unconfirmed: {path}"
        raise EvaluationReceiptUnconfirmedError(problem) from error


class EvaluationRecoveryBlockedError(RuntimeError):
    """Recovery retains ownership until the named execution service supplies evidence."""

    def __init__(self, block: pb.EvaluationRecoveryBlock) -> None:
        validate(block)
        self.block = block
        super().__init__(
            "Recovery blocked: previous writers may still modify this workspace; "
            f"environment={block.environment}, backing={block.backing}, "
            f"outstanding={','.join(block.outstanding)}; {block.prerequisite}"
        )


class EvaluationWriterReconciliation(Protocol):
    """Require environment verification before acquiring an inherited evaluation backing.

    The implementation owns verification of the complete writer set. Returning
    permits acquisition; missing or unsupported evidence raises the typed block.
    No receipt, timeout, or terminal evaluation result implements this role.
    """

    async def require_stopped(self, backing: str, outstanding: tuple[str, ...]) -> None: ...


def recovery_block(
    environment: str, backing: str, handle: str, prerequisite: str
) -> pb.EvaluationRecoveryBlock:
    """Name the retained obligation and the authoritative evidence still missing."""
    return pb.EvaluationRecoveryBlock(
        environment=environment, backing=backing, outstanding=[handle], prerequisite=prerequisite
    )


class EvaluationWorkspaceAcquisitions:
    """Guard every evaluation acquisition against the durable inherited writer manifest."""

    def __init__(
        self,
        namespace: EvaluationStateNamespace,
        environment: str,
        reconciliation: EvaluationWriterReconciliation | None = None,
    ) -> None:
        self._namespace, self._environment, self._reconciliation = (
            namespace,
            environment,
            reconciliation,
        )
        self._path = f"{environment}-evaluation-acquisitions.pb"

    async def require(
        self, handle: str, backing: str, *, fresh: bool, live: tuple[str, ...]
    ) -> None:
        contents = self._namespace.read_bytes(self._path)
        grants = (
            pb.EvaluationWorkspaceAcquisitions()
            if contents is None
            else pb.EvaluationWorkspaceAcquisitions.from_binary(contents)
        )
        validate(grants)
        inherited = tuple(name for name in grants.handles if name not in live)
        if not fresh and handle not in inherited:
            inherited = (*inherited, handle)
        if self._reconciliation is not None:
            await self._reconciliation.require_stopped(backing, inherited)
        elif inherited:
            block = pb.EvaluationRecoveryBlock(
                environment=self._environment,
                backing=backing,
                outstanding=list(inherited),
                prerequisite="environment verification of the complete inherited writer set is unsupported",
            )
            validate(block)
            _write_receipt(self._namespace, self._path + ".blocked", block.to_binary())
            raise EvaluationRecoveryBlockedError(block)
        if handle not in grants.handles:
            grants.handles.append(handle)
            _write_receipt(self._namespace, self._path, grants.to_binary())


class LocalEvaluationReceipts:
    """Retain local stage starts, results and cleanup obligations across fresh owners.

    Writes use Project's atomic durable byte boundary. A receipt is evidence,
    never permission to repeat an interrupted stage or forget its workspace.
    """

    def __init__(
        self,
        namespace: EvaluationStateNamespace,
        reconciliation: EvaluationWriterReconciliation | None = None,
    ) -> None:
        self._namespace = namespace
        self._acquisitions = EvaluationWorkspaceAcquisitions(namespace, "local", reconciliation)

    async def require_acquisition(self, handle: str, backing: str, live: tuple[str, ...]) -> None:
        await self._acquisitions.require(handle, backing, fresh=True, live=live)

    @staticmethod
    def _path(handle: str) -> str:
        return f"local-evaluations/{hashlib.sha256(handle.encode()).hexdigest()}.pb"

    def load(self, handle: str) -> pb.LocalEvaluationReceipt | None:
        contents = self._namespace.read_bytes(self._path(handle))
        if contents is None:
            return None
        receipt = pb.LocalEvaluationReceipt.from_binary(contents)
        validate(receipt)
        if receipt.handle != handle:
            problem = f"local evaluation receipt belongs to another handle: {handle}"
            raise ExecutorRejectedError(problem)
        return receipt

    def save(self, receipt: pb.LocalEvaluationReceipt) -> None:
        validate(receipt)
        _write_receipt(self._namespace, self._path(receipt.handle), receipt.to_binary())

    def begin(self, handle: str, request: EvaluationRequest, snapshot: str, backing: str) -> bool:
        """Persist acceptance before the task exists; refuse a different request on replay."""
        digest = evaluation_work_digest(request)
        prior = self.load(handle)
        if prior is not None:
            if prior.request_digest != digest:
                problem = f"local evaluation handle {handle!r} has different work"
                raise ExecutorRejectedError(problem)
            return False
        self.save(
            pb.LocalEvaluationReceipt(
                handle=handle,
                request_digest=digest,
                snapshot=snapshot,
                backing=backing,
                stages=[step.name for step in request.stages],
                observation=pb.LocalEvaluationObservation(state=pb.LocalEvaluationState.QUEUED),
            )
        )
        return True

    def update(
        self,
        handle: str,
        *,
        cleanup_required: bool | None = None,
        workspace: str | None = None,
        cleaned: bool | None = None,
        observation: pb.LocalEvaluationObservation | None = None,
    ) -> None:
        """Write the next evidence transition, preserving all outstanding obligations."""
        receipt = self.load(handle)
        if receipt is None:
            problem = f"missing local evaluation receipt: {handle}"
            raise ExecutorRejectedError(problem)
        if cleanup_required is not None:
            receipt.cleanup_required = cleanup_required
        if workspace is not None:
            receipt.workspace = workspace
        if cleaned is not None:
            receipt.cleaned = cleaned
        if observation is not None:
            receipt.observation = observation
        self.save(receipt)

    def start_cleanup(self, handle: str) -> None:
        receipt = self.load(handle)
        if receipt is None:
            problem = f"missing local evaluation receipt: {handle}"
            raise ExecutorRejectedError(problem)
        receipt.cleanup_started = True
        self.save(receipt)

    def start_stage(self, handle: str, name: str) -> None:
        receipt = self.load(handle)
        if receipt is None:
            problem = f"missing local evaluation receipt: {handle}"
            raise ExecutorRejectedError(problem)
        receipt.started_stages.append(name)
        self.save(receipt)

    def observe(self, handle: str, observation: ExecutorObservation) -> None:
        receipt = self.load(handle)
        if receipt is None or receipt.observation is None:
            problem = f"missing local evaluation receipt: {handle}"
            raise ExecutorRejectedError(problem)
        held = _decode_observation(receipt.observation, handle)
        joined = join_observation(held, observation)
        native = from_mapping(pb.LocalEvaluationObservation, joined.model_dump(mode="json"))
        self.update(handle, observation=native)

    def blocked(self, handle: str) -> EvaluationRecoveryBlockedError:
        receipt = self.load(handle)
        backing = receipt.backing if receipt is not None else handle
        block = recovery_block(
            "local",
            backing,
            handle,
            "restore the owning execution service to verify writer termination and workspace cleanup",
        )
        _write_receipt(self._namespace, self._path(handle) + ".blocked", block.to_binary())
        return EvaluationRecoveryBlockedError(block)

    def observation(self, handle: str) -> ExecutorObservation | None:
        """Only confirmed cleanup makes a retained terminal result releasable."""
        receipt = self.load(handle)
        if receipt is None:
            return None
        if not receipt.cleaned:
            raise self.blocked(handle)
        if receipt.observation is None:
            raise self.blocked(handle)
        return _decode_observation(receipt.observation, handle)


def _decode_observation(native: pb.LocalEvaluationObservation, handle: str) -> ExecutorObservation:
    # uint64 fields are decimal strings on the neutral protobuf boundary.
    value = to_mapping(native)
    steps = cast("list[dict[str, object]]", value["stage_results"])
    for step in steps:
        evidence = cast("dict[str, object] | None", step["result"])
        if evidence is not None:
            evidence["accepted_round"] = int(str(evidence["accepted_round"]))
            partial = cast("dict[str, object] | None", evidence["partial_measurement"])
            if partial is not None and partial["progress"] is not None:
                progress = cast("dict[str, object]", partial["progress"])
                progress["completed"] = int(str(progress["completed"]))
                progress["required"] = int(str(progress["required"]))
    decoded = decode_json(
        json.dumps(value),
        ExecutorObservation,
        source=f"local evaluation {handle}",
        max_bytes=16 * 1024 * 1024,
    )
    if isinstance(decoded, Unreadable):
        raise ExecutorRejectedError(str(decoded))
    return decoded


__all__ = [
    "EvaluationReceiptUnconfirmedError",
    "EvaluationRecoveryBlockedError",
    "EvaluationWorkspaceAcquisitions",
    "EvaluationWriterReconciliation",
    "LocalEvaluationReceipts",
]
