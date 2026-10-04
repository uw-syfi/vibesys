"""Owners of the evidence operations: reading evidence back, and retaining a proven revision.

Both consume ``EvidenceRef`` values that core's ledger embedded in the request, so
neither looks evidence up by a bare id. ``InterpretEvidenceOwner`` maps each
reference through ``EvidenceLookup`` to the reading the strategy decodes. A
reference the ledger never recorded, or recorded for another purpose, makes the
whole outcome ``rejected`` with no readings: never a guess.
``RetainRevisionOwner`` accepts a revision only when the embedded accuracy proof
is successful correctness evidence of exactly that revision, then retains it
through the workspace.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from vs_core.api import ContractError, EvidenceKind, EvidenceRef, ObservationStatus
from vs_evaluation.api import EvidenceOutcome
from vs_runtime._operation_catalog import Applied, Inspection, NotApplied
from vs_runtime.contracts import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import OperationRequest, RevisionRef
    from vs_evaluation.api import TrustedEvidence
    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime._evidence_ledger import EvidenceLookup
    from vs_runtime.contracts import RevisionLedger, Workspaces


class Refusal(StrEnum):
    """Why a retention was refused. The value is the receipt detail."""

    NOT_ACCURACY_PROOF = "not_accuracy_proof"
    PROOF_NAMES_OTHER_REVISION = "proof_names_other_revision"
    REVISION_NOT_CANONICAL = "revision_not_canonical"
    REVISION_UNKNOWN = "revision_unknown"


class InterpretRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    evidence: tuple[EvidenceRef, ...]


class RetainRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    revision: RevisionRef
    accuracy_proof: EvidenceRef


def _refs(request: OperationRequest, field: str) -> tuple[EvidenceRef, ...]:
    value = getattr(request, field, None)
    items = value if isinstance(value, tuple) else (value,)
    if not items or not all(isinstance(item, EvidenceRef) for item in items):
        raise ContractError((field,), "request must embed core EvidenceRef values")
    return cast("tuple[EvidenceRef, ...]", items)


class InterpretEvidenceOwner:
    """Read each embedded evidence reference back from the evidence ledger."""

    def __init__(self, lookup: EvidenceLookup) -> None:
        """Bind the read port of the evidence ledger."""
        self._lookup = lookup

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """One reading per reference, in request order, or a refusal naming the first bad one."""
        del context
        return self._interpret(request)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """A query has no effect to prove, so inspection answers by reading again."""
        del context
        return Applied(self._interpret(request))

    def _interpret(self, request: OperationRequest) -> Mapping[str, object]:
        readings: list[Mapping[str, object]] = []
        for ref in _refs(request, "evidence"):
            entry = self._lookup.lookup(ref.source_request, ref.evidence_id)
            if entry is None:
                return _rejected_readings()
            if entry.purpose != ref.purpose:
                return _rejected_readings()
            readings.append(_reading(ref, entry.evidence))
        return {"status": "succeeded", "readings": tuple(readings)}


def _rejected_readings() -> Mapping[str, object]:
    """The declared outcome has no detail field, so the refusal is its ``rejected`` status."""
    return {"status": "rejected", "readings": ()}


def _reading(ref: EvidenceRef, evidence: TrustedEvidence) -> Mapping[str, object]:
    partial = evidence.partial_measurement
    progress = None if partial is None else partial.progress
    return {
        "evidence_id": ref.evidence_id,
        "kind": ref.kind,
        "passed": evidence.outcome is not EvidenceOutcome.FAILED,
        "stage": evidence.stage_name,
        "protocol": evidence.result_protocol,
        "metrics": tuple(
            {
                "name": metric.name,
                "value": metric.value,
                "direction": metric.direction or "max",
                "unit": metric.unit,
            }
            for metric in evidence.metrics
        ),
        "partial": None
        if partial is None
        else {
            "name": partial.name,
            "value": partial.value,
            "direction": partial.direction,
            "unit": partial.unit,
            "target": partial.target,
            "completed": None if progress is None else progress.completed,
            "required": None if progress is None else progress.required,
            "progress_unit": None if progress is None else progress.unit,
        },
        "feedback": evidence.semantic_summary or "",
    }


class RetainRevisionOwner:
    """Retain a revision once its embedded accuracy proof names exactly it."""

    def __init__(
        self,
        workspaces: Workspaces,
        ledger: RevisionLedger,
        commit_of: Callable[[RevisionRef], str | None],
        label: str,
    ) -> None:
        """Bind the workspaces, the retention ledger, the reference encoding and the label."""
        self._workspaces = workspaces
        self._ledger = ledger
        self._commit_of = commit_of
        self._label = label

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Retain through the workspace, or refuse with the typed reason."""
        del context
        commit, refusal = self._validated(request)
        if commit is None:
            return _receipt(retained=False, detail=refusal)
        if not await self._ledger.retains(commit):
            try:
                await self._workspaces.root.retain(commit, label=self._label)
            except (ValueError, RuntimeContractError):
                return _receipt(retained=False, detail=Refusal.REVISION_UNKNOWN)
        return _receipt(retained=True, detail=None)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Applied when the proven revision is already retained, else provably not applied."""
        del context
        commit, _ = self._validated(request)
        if commit is not None and await self._ledger.retains(commit):
            return Applied(_receipt(retained=True, detail=None))
        return NotApplied()

    def _validated(self, request: OperationRequest) -> tuple[str | None, Refusal | None]:
        retain = cast("RetainRequest", request)
        (proof,) = _refs(request, "accuracy_proof")
        if (
            proof.kind is not EvidenceKind.CORRECTNESS
            or proof.status is not ObservationStatus.SUCCEEDED
        ):
            return None, Refusal.NOT_ACCURACY_PROOF
        if proof.candidate != retain.revision:
            return None, Refusal.PROOF_NAMES_OTHER_REVISION
        commit = self._commit_of(retain.revision)
        if commit is None:
            return None, Refusal.REVISION_NOT_CANONICAL
        return commit, None


def _receipt(*, retained: bool, detail: Refusal | None) -> Mapping[str, object]:
    return {
        "status": "succeeded" if retained else "rejected",
        "retained": retained,
        "detail": "" if detail is None else detail.value,
    }
