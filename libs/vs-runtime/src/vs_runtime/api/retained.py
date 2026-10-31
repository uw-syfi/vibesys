"""Permanent read-only receipt codecs for semantic import, with no executor dispatch.

Known receipt families use their owning strict schemas. A sealed execution is decoded
by its retained type tag, never by importing a stored Python name. Untagged historical
executions use the disjoint union of the three retained result shapes. Unknown paths,
tags and keys fail with a source diagnostic. No file reads or receipt writes occur.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from vs_core.api import AttemptRef, HostFence, Observation
from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_runtime._access_settlement import AccessReceipt, UnsettledInvocations
from vs_runtime._core_requests import ExecutionResult
from vs_runtime._effect_fence import _FenceRecord
from vs_runtime._evidence_ledger import EvidenceEntry
from vs_runtime._job_registry import JobRecord, ScopeJobs
from vs_runtime._operation_receipts import RecordReceipt, ResultReceipt
from vs_runtime._owed_submissions import _Entries
from vs_runtime._receipt_store import ExecutionPhase, ExecutionRecord
from vs_runtime._semantic_events import Published
from vs_runtime._session_requests import DispatchRecord, SessionBinding
from vs_runtime._workspace_receipts import (
    AttemptBinding,
    Released,
    RevisionOwners,
    RootBaseline,
)

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

__all__ = ["RetainedExternalIdentity", "RetainedReceipt", "RetainedReceiptError", "decode_receipt"]


class RetainedReceiptError(ValueError):
    """A source receipt is corrupt or from an unknown retained format."""

    def __init__(self, path: str, detail: str) -> None:
        """Identify the document and the prerequisite it failed."""
        super().__init__(f"{path}: {detail}")


@dataclass(frozen=True, slots=True)
class RetainedExternalIdentity:
    """An identity to inspect, carrying no authority to repeat a request."""

    kind: str
    identity: str
    source_request: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedReceipt:
    """Validated source values and external identities, free of live resources."""

    values: tuple[BaseModel, ...]
    external_identities: tuple[RetainedExternalIdentity, ...] = ()


_READERS = {
    ("executions", "execution"): ExecutionRecord,
    ("fences", "fence"): HostFence,
    ("workspace-bindings", "binding"): AttemptBinding,
    ("workspace-root", "holder"): AttemptRef,
    ("workspace-root-baseline", "baseline"): RootBaseline,
    ("workspace-generations", "generation"): AttemptRef,
    ("workspace-revisions", "owners"): RevisionOwners,
    ("workspace-released", "released"): Released,
    ("session-bindings", "binding"): SessionBinding,
    ("session-dispatches", "dispatch"): DispatchRecord,
    ("operations", "record"): RecordReceipt,
    ("observations", "observation"): Observation,
    ("agent-owed-submissions", "owed"): _Entries,
    ("session-access", "access"): AccessReceipt,
    ("session-access-unsettled", "workspace"): UnsettledInvocations,
    ("effect-fences", "fence"): _FenceRecord,
    ("evaluation-jobs", "job"): JobRecord,
    ("evaluation-scopes", "scope"): ScopeJobs,
    ("evidence-ledger", "evidence"): EvidenceEntry,
}
_RESULTS = {
    "vs_runtime._core_requests.ExecutionResult": TypeAdapter(ExecutionResult),
    "vs_runtime._operation_receipts.ResultReceipt": TypeAdapter(ResultReceipt),
    "vs_runtime._semantic_events.Published": TypeAdapter(Published),
    None: TypeAdapter(ExecutionResult | ResultReceipt | Published),
}
_PATH = re.compile(r"(?P<family>[a-z-]+)/(?P<digest>[0-9a-f]{64})\.(?P<part>[a-z]+)\.json")
_MAX_BYTES = 128 * 1024 * 1024


def _decode[T](
    data: bytes | str, reader: TypeAdapter[T], path: str, registry: OperationRegistry
) -> T:
    value = decode_json(
        data,
        reader,
        source=path,
        max_bytes=_MAX_BYTES,
        options=ValidationOptions(
            context={"operation_registry": registry, "persisted_operation": True}
        ),
    )
    if isinstance(value, Unreadable):
        raise RetainedReceiptError(path, str(value))
    return value


def decode_receipt(path: str, data: bytes, registry: OperationRegistry) -> RetainedReceipt:
    """Decode a namespace-relative receipt path, retaining unknown-effect obligations."""
    match = _PATH.fullmatch(path)
    reader = None if match is None else _READERS.get((match["family"], match["part"]))
    if reader is None:
        raise RetainedReceiptError(path, "unknown receipt family or filename")
    value = _decode(data, TypeAdapter(reader), path, registry)
    nested: tuple[BaseModel, ...] = ()
    if isinstance(value, ExecutionRecord):
        if value.phase == ExecutionPhase.BEGUN:
            return RetainedReceipt(
                (value,), (RetainedExternalIdentity("unsealed-execution", path),)
            )
        if value.result_json is None or value.result_type not in _RESULTS:
            raise RetainedReceiptError(
                f"{path}.result_type", "missing result or unknown retained type"
            )
        result = _decode(
            value.result_json, _RESULTS[value.result_type], f"{path}.result_json", registry
        )
        nested = (result,)
        if isinstance(result, ResultReceipt) and result.outcome_json is not None:
            nested = (*nested, registry.decode_outcome(result.schema_ref, result.outcome_json))
    return RetainedReceipt((value, *nested), _external(value))


def _external(value: BaseModel) -> tuple[RetainedExternalIdentity, ...]:
    if isinstance(value, JobRecord):
        result = (RetainedExternalIdentity("evaluation", value.executor_handle, value.request_id),)
    elif isinstance(value, SessionBinding):
        result = (
            RetainedExternalIdentity("session", value.session_key, value.ensure_request.root),
        )
    elif isinstance(value, DispatchRecord):
        result = (
            RetainedExternalIdentity("invocation", value.request_id.root, value.request_id.root),
        )
    elif isinstance(value, AttemptBinding):
        result = (
            RetainedExternalIdentity(
                "workspace", value.resource_id.root, value.ensure_request.root
            ),
        )
    elif isinstance(value, _Entries):
        result = tuple(
            RetainedExternalIdentity("owed-evaluation", entry.call_id) for entry in value.entries
        )
    elif isinstance(value, AccessReceipt) and not value.settled:
        result = (RetainedExternalIdentity("unsettled-access", value.workspace_path),)
    elif isinstance(value, UnsettledInvocations):
        result = tuple(
            RetainedExternalIdentity("unsettled-invocation", key.text) for key in value.keys
        )
    elif isinstance(value, _FenceRecord):
        result = tuple(
            RetainedExternalIdentity(
                "unsettled-effect", entry.request_id.root, entry.request_id.root
            )
            for entry in value.entries
            if not entry.left
        )
    else:
        result = ()
    return result
