"""Pure operation schema registration, wire decoding and startup validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types.common import Capabilities, LifecycleClass, OperationDescriptor, OperationSchemaRef
from .types.intents import ExecuteRegisteredOperation, OperationWire
from .types.strategy import Decision, Operation, StrategyState

ENVELOPE_SCHEMA_VERSION = 1

if TYPE_CHECKING:
    from pydantic import BaseModel

    from .types.kernel import CoreState, RunEnvelope
    from .types.strategy import OperationRequest, StrategyDeclaration


class ContractError(ValueError):
    """Strict ingress or startup error naming the offending contract path."""

    def __init__(self, path: tuple[str | int, ...], detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{'.'.join(map(str, path))}: {detail}")


@dataclass(frozen=True)
class OperationRegistration:
    """Owning-library model classes, kept outside persisted core state."""

    descriptor: OperationDescriptor
    request_model: type[OperationRequest]
    outcome_model: type[BaseModel]


class OperationRegistry:
    """Closed registered codec; changed schemas require explicit migration."""

    def __init__(self, registrations: tuple[OperationRegistration, ...] = ()) -> None:
        self._registrations = registrations
        seen: set[str] = set()
        for index, registration in enumerate(registrations):
            descriptor = registration.descriptor
            if descriptor.kind in seen:
                raise ContractError(("registry", index, "kind"), "duplicate operation kind")
            seen.add(descriptor.kind)
            fields = registration.request_model.model_fields
            if fields["kind"].default != descriptor.kind:
                raise ContractError(("registry", index, "kind"), "request kind mismatch")
            if fields["lifecycle"].default != descriptor.lifecycle:
                raise ContractError(("registry", index, "lifecycle"), "request lifecycle mismatch")
            if registration.request_model.outcome_model is not registration.outcome_model:
                raise ContractError(("registry", index, "outcome_schema"), "outcome model mismatch")

    @property
    def descriptors(self) -> tuple[OperationDescriptor, ...]:
        """Pure descriptor values safe to persist and project."""
        return tuple(entry.descriptor for entry in self._registrations)

    def _find(self, schema: OperationSchemaRef) -> OperationRegistration:
        for entry in self._registrations:
            if entry.descriptor.kind == schema.kind:
                if (
                    OperationSchemaRef(
                        kind=entry.descriptor.kind,
                        request_schema=entry.descriptor.request_schema,
                        outcome_schema=entry.descriptor.outcome_schema,
                        lifecycle=entry.descriptor.lifecycle,
                    )
                    == schema
                ):
                    return entry
                raise ContractError(
                    ("operation", schema.kind, "schema"), "schema changed; migration required"
                )
        raise ContractError(("operation", schema.kind), "unregistered kind")

    def encode(self, request: OperationRequest) -> OperationWire:
        """Validate the exact registered subtype before writing durable wire data."""
        entry = next(
            (entry for entry in self._registrations if entry.descriptor.kind == request.kind), None
        )
        if entry is None:
            raise ContractError(("operation", request.kind), "unregistered kind")
        if type(request) is not entry.request_model:
            raise ContractError(("operation", request.kind), "unregistered request model")
        descriptor = entry.descriptor
        schema = OperationSchemaRef(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=descriptor.lifecycle,
        )
        payload = entry.request_model.model_validate_json(request.model_dump_json())
        return OperationWire(schema_ref=schema, payload_json=payload.model_dump_json())

    def decode(self, wire: OperationWire) -> OperationRequest:
        """Restore the original request subtype, with no base-model narrowing."""
        entry = self._find(wire.schema_ref)
        return entry.request_model.model_validate_json(wire.payload_json)

    def decode_outcome(self, schema: OperationSchemaRef, payload_json: str) -> BaseModel:
        """Give the strategy the owning library's validated outcome value."""
        return self._find(schema).outcome_model.model_validate_json(payload_json)

    def decode_payload(self, payload: dict[str, object]) -> OperationRequest:
        """Decode a JSON operation field using its exact registered kind."""
        kind = payload.get("kind")
        entry = next(
            (entry for entry in self._registrations if entry.descriptor.kind == kind), None
        )
        if entry is None:
            raise ContractError(("operation", str(kind)), "unregistered kind")
        return entry.request_model.model_validate_json(json.dumps(payload))

    def validate_decision(self, decision: Decision) -> Decision:
        """The shell validates operation proposals before calling step."""
        if not isinstance(decision, Operation):
            return decision
        self.encode(decision.request)
        return Operation.model_validate_json(
            decision.model_dump_json(), context={"operation_registry": self}
        )

    def encode_envelope(self, envelope: RunEnvelope) -> str:
        """Write the whole atomic envelope with registered operation subtypes."""
        self.validate_core(envelope.core)
        return envelope.model_dump_json()

    def decode_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], source: str
    ) -> RunEnvelope[S]:
        """Resume only with matching schemas and the same registered codec."""
        envelope = model.model_validate_json(source, context={"operation_registry": self})
        if envelope.schema_version != ENVELOPE_SCHEMA_VERSION:
            raise ContractError(("schema_version",), "explicit envelope migration required")
        self.validate_core(envelope.core)
        return envelope

    def validate_core(self, state: CoreState) -> None:
        """Check every durable descriptor and request before replay."""
        for descriptor in state.registry:
            schema = OperationSchemaRef(
                kind=descriptor.kind,
                request_schema=descriptor.request_schema,
                outcome_schema=descriptor.outcome_schema,
                lifecycle=descriptor.lifecycle,
            )
            self._find(schema)
        for intent in state.intents.intents:
            if isinstance(intent.request, ExecuteRegisteredOperation):
                self.decode(intent.request.operation)


def _validate_operation(descriptor: OperationDescriptor, path: tuple[str | int, ...]) -> None:
    if (
        descriptor.lifecycle
        in (LifecycleClass.IDEMPOTENT_WRITE, LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN)
        and not descriptor.inspect
    ):
        raise ContractError((*path, "inspect"), "lifecycle requires inspect")
    if descriptor.lifecycle in (LifecycleClass.OWNED_JOB, LifecycleClass.SESSION_TURN) and not (
        descriptor.cancel and descriptor.watch
    ):
        raise ContractError((*path, "cancel/watch"), "owned lifecycle requires cancel and watch")


def validate_startup(declaration: StrategyDeclaration, available: Capabilities) -> Capabilities:
    """Reject required absence; expose only the declared validated intersection."""
    if declaration.required & declaration.optional:
        raise ContractError(("declaration", "optional"), "capability also required")
    missing = declaration.required - available.lifecycle
    if missing:
        raise ContractError(("capabilities", sorted(missing)[0]), "required capability unavailable")
    selected = _select_operations(declaration, available)
    return Capabilities(
        lifecycle=available.lifecycle & (declaration.required | declaration.optional),
        operations=selected,
    )


def _select_operations(
    declaration: StrategyDeclaration, available: Capabilities
) -> tuple[OperationDescriptor, ...]:
    offered: dict[str, OperationDescriptor] = {}
    for index, descriptor in enumerate(available.operations):
        if descriptor.kind in offered:
            raise ContractError(("operations", index, "kind"), "duplicate operation kind")
        _validate_operation(descriptor, ("operations", index))
        offered[descriptor.kind] = descriptor
    declared: set[str] = set()
    selected: list[OperationDescriptor] = []
    for required, references in (
        (True, declaration.required_operations),
        (False, declaration.optional_operations),
    ):
        for index, reference in enumerate(references):
            path = ("required_operations" if required else "optional_operations", index)
            if reference.kind in declared:
                raise ContractError((*path, "kind"), "duplicate declaration")
            declared.add(reference.kind)
            descriptor = offered.get(reference.kind)
            if descriptor is None:
                if required:
                    raise ContractError((*path, reference.kind), "required operation unavailable")
                continue
            projected = OperationSchemaRef(
                kind=descriptor.kind,
                request_schema=descriptor.request_schema,
                outcome_schema=descriptor.outcome_schema,
                lifecycle=descriptor.lifecycle,
            )
            if reference != projected:
                raise ContractError((*path, "schema"), "operation schema/lifecycle mismatch")
            selected.append(descriptor)
    return tuple(selected)
