"""Closed catalog of registered operations and the owner implementations behind them.

The catalog is the runtime half of an operation registry. The registry (in
vs-core) only encodes and decodes a request; this catalog says which owner
implementation performs each declared operation and what it does when it cannot.
Every entry is keyed by the operation's exact declared schema (kind, request
schema, outcome schema, lifecycle), so a changed schema never reaches an owner
written for the old one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_core.api import (
    ContractError,
    OperationRegistration,
    OperationRegistry,
    OperationSchemaRef,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_runtime._core_requests import ExecutionContext


@dataclass(frozen=True)
class Applied:
    """The owner proves the effect landed and supplies the outcome it produced."""

    outcome: BaseModel | Mapping[str, object]


@dataclass(frozen=True)
class NotApplied:
    """The owner proves the effect never happened, so performing it is safe."""


@dataclass(frozen=True)
class Indeterminate:
    """The owner cannot tell whether the effect landed; nothing may be repeated."""

    reason: str


type Inspection = Applied | NotApplied | Indeterminate


@dataclass(frozen=True)
class Cancelled:
    """The owner stopped the operation. This never says its resources are released."""

    detail: str = ""


class OperationOwner(Protocol):
    """The owning library's implementation of one declared operation.

    ``execute`` returns the outcome as a model of the registered outcome class or
    as a mapping of its fields. The executor validates it against the declared
    schema, so an owner never builds a core observation. ``inspect`` answers from
    durable facts only and never performs the effect.
    """

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> BaseModel | Mapping[str, object]: ...

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection: ...


@runtime_checkable
class CancellableOwner(OperationOwner, Protocol):
    """An owner whose operation declares cancellation."""

    async def cancel(self, request: OperationRequest, context: ExecutionContext) -> Cancelled: ...


class RefusalReason(StrEnum):
    """Why a declared operation has no owner yet. Each member names a missing contract."""

    NO_EVIDENCE_LOOKUP = "no_evidence_lookup"
    NO_ACCURACY_PROOF = "no_accuracy_proof"


@dataclass(frozen=True)
class OperationEntry:
    """One declared operation, with an owner or with a typed reason it has none."""

    registration: OperationRegistration
    owner: OperationOwner | None = None
    refusal: RefusalReason | None = None
    refusal_detail: str = ""

    def __post_init__(self) -> None:
        """Exactly one of owner and refusal, and cancellation only when it is implemented."""
        kind = self.registration.descriptor.kind
        if (self.owner is None) == (self.refusal is None):
            raise ContractError(("catalog", kind), "exactly one of owner and refusal is required")
        if (
            self.owner is not None
            and self.registration.descriptor.cancel
            and not isinstance(self.owner, CancellableOwner)
        ):
            raise ContractError(("catalog", kind, "cancel"), "declared cancel needs owner.cancel")

    @classmethod
    def owned(cls, registration: OperationRegistration, owner: OperationOwner) -> OperationEntry:
        """Bind an owner implementation to its declared operation."""
        return cls(registration, owner=owner)

    @classmethod
    def refused(
        cls, registration: OperationRegistration, reason: RefusalReason, detail: str
    ) -> OperationEntry:
        """Declare the operation without an owner. Execution reports a typed rejection."""
        return cls(registration, refusal=reason, refusal_detail=detail)

    @property
    def schema(self) -> OperationSchemaRef:
        """The exact declared schema reference this entry serves."""
        descriptor = self.registration.descriptor
        return OperationSchemaRef(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=descriptor.lifecycle,
        )


class OperationCatalog:
    """Registry-checked, closed map from declared schema to entry.

    Construction fails, naming the kind, when an entry is not registered, a kind
    repeats, or a registered kind has no entry, so wiring drift is caught at
    startup and never during a run. Lookup of any other schema returns None.
    """

    def __init__(self, registry: OperationRegistry, entries: tuple[OperationEntry, ...]) -> None:
        """Validate the entries against the registry's declared descriptors."""
        by_kind: dict[str, OperationEntry] = {}
        declared = {descriptor.kind: descriptor for descriptor in registry.descriptors}
        for index, entry in enumerate(entries):
            kind = entry.registration.descriptor.kind
            if kind in by_kind:
                raise ContractError(("catalog", index, "kind"), f"duplicate operation {kind!r}")
            if declared.get(kind) != entry.registration.descriptor:
                raise ContractError(("catalog", index, "kind"), f"{kind!r} is not registered")
            by_kind[kind] = entry
        missing = sorted(set(declared) - set(by_kind))
        if missing:
            raise ContractError(("catalog",), f"registered operations without entry: {missing}")
        self._registry = registry
        self._entries = by_kind

    @property
    def registry(self) -> OperationRegistry:
        """The codec these entries were checked against."""
        return self._registry

    @property
    def entries(self) -> tuple[OperationEntry, ...]:
        """Every entry, in kind order."""
        return tuple(self._entries[kind] for kind in sorted(self._entries))

    def find(self, schema: OperationSchemaRef) -> OperationEntry | None:
        """The entry whose declared schema equals ``schema`` exactly, else None."""
        entry = self._entries.get(schema.kind)
        return entry if entry is not None and entry.schema == schema else None
