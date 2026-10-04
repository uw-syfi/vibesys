"""Pure operation schema registration, wire decoding and startup validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args, get_origin, overload

from ._outcomes import prove_outcome
from ._values import (
    ImmutableSchemaError,
    canonical_json,
    deeply_immutable,
    validate_immutable_schema,
)
from .types.common import (
    Capabilities,
    ExecuteRegisteredOperation,
    LifecycleClass,
    OperationDescriptor,
    OperationId,
    OperationNormalizationKind,
    OperationSchemaRef,
    OperationWire,
    ScopeReopenNormalization,
)
from .types.evaluation import MeasurementIdentity
from .types.intents import OperationResult, RequestObserved
from .types.sessions import TurnSpec
from .types.strategy import Decision, Operation, StrategyState

ENVELOPE_SCHEMA_VERSION = 3

if TYPE_CHECKING:
    from collections.abc import Callable

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
    normalize_turn: Callable[[OperationRequest], TurnSpec] | None = None
    normalize_scope_reopen: Callable[[OperationRequest], ScopeReopenNormalization] | None = None
    normalize_measurement: Callable[[OperationRequest], MeasurementIdentity] | None = None


@dataclass(frozen=True)
class OperationMigration:
    """Explicit pure conversion chosen by the owning library, outside core state."""

    source: OperationSchemaRef
    target: OperationSchemaRef
    rewrite: Callable[[str], str]


@dataclass(frozen=True)
class EnvelopeMigration:
    """Explicit pure conversion, never an implicit decode fallback."""

    source_version: int
    target_version: int
    rewrite: Callable[[str], str]


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
            _validate_normalizer(registration, index)
            fields = registration.request_model.model_fields
            for tag, expected in (("kind", descriptor.kind), ("lifecycle", descriptor.lifecycle)):
                annotation = fields[tag].annotation
                if get_origin(annotation) is not Literal or get_args(annotation) != (expected,):
                    raise ContractError(("registry", index, tag), "closed Literal tag required")
            for model in (registration.request_model, registration.outcome_model):
                try:
                    validate_immutable_schema(model)
                except ImmutableSchemaError as error:
                    raise ContractError(("registry", index), str(error)) from error
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
        if not deeply_immutable(request):
            raise ContractError(("operation", request.kind), "immutable registered value required")
        descriptor = entry.descriptor
        schema = OperationSchemaRef(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=descriptor.lifecycle,
        )
        payload = entry.request_model.model_validate_json(request.model_dump_json())
        return OperationWire(schema_ref=schema, payload_json=canonical_json(payload))

    def normalize_turn(self, request: OperationRequest) -> TurnSpec | None:
        """Normalize the validated registered payload into strict immutable turn data."""
        wire = self.encode(request)
        entry = self._find(wire.schema_ref)
        if entry.normalize_turn is None:
            return None
        return _normalize(
            self.decode(wire),
            entry.normalize_turn,
            TurnSpec,
            ("operation", request.kind, "normalize_turn"),
        )

    def normalize_measurement(self, request: OperationRequest) -> MeasurementIdentity | None:
        """Bind expected measurement identity to the canonical registered payload.

        Only owned jobs can expose measurement authority. Absence remains
        nonmeasurement work and never borrows submitted evidence as its plan.
        """
        wire = self.encode(request)
        entry = self._find(wire.schema_ref)
        if entry.normalize_measurement is None:
            return None
        return _normalize(
            self.decode(wire),
            entry.normalize_measurement,
            MeasurementIdentity,
            ("operation", request.kind, "normalize_measurement"),
        )

    def normalize_scope_reopen(self, request: OperationRequest) -> ScopeReopenNormalization | None:
        """Normalize the canonical payload into guarded reopening data, with no episode choice."""
        wire = self.encode(request)
        entry = self._find(wire.schema_ref)
        if entry.normalize_scope_reopen is None:
            return None
        return _normalize(
            self.decode(wire),
            entry.normalize_scope_reopen,
            ScopeReopenNormalization,
            ("operation", request.kind, "normalize_scope_reopen"),
        )

    def decode(self, wire: OperationWire) -> OperationRequest:
        """Restore the original request subtype, with no base-model narrowing."""
        entry = self._find(wire.schema_ref)
        return entry.request_model.model_validate_json(wire.payload_json)

    def migrate_operation(
        self, wire: OperationWire, migration: OperationMigration
    ) -> OperationWire:
        """Convert only the exact declared source, then validate registered target."""
        if wire.schema_ref != migration.source:
            raise ContractError(("migration", "source"), "operation source schema mismatch")
        if (
            migration.source.kind != migration.target.kind
            or migration.source.lifecycle != migration.target.lifecycle
        ):
            raise ContractError(("migration", "kind"), "operation identity cannot change")
        target = OperationWire(
            schema_ref=migration.target, payload_json=migration.rewrite(wire.payload_json)
        )
        self.decode(target)
        return target

    def migrate_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], source: str, migration: EnvelopeMigration
    ) -> RunEnvelope[S]:
        """Apply a selected version conversion and strictly validate the full result."""
        version = _read_envelope_version(source, ("migration", "source"))
        if version != migration.source_version:
            raise ContractError(("migration", "source"), "envelope source version mismatch")
        if migration.target_version != ENVELOPE_SCHEMA_VERSION:
            raise ContractError(("migration", "target"), "unregistered envelope target version")
        transformed = migration.rewrite(source)
        envelope = self.decode_envelope(model, transformed)
        if envelope.schema_version != migration.target_version:
            raise ContractError(("migration", "target"), "envelope result version mismatch")
        return envelope

    def encode_outcome(self, schema: OperationSchemaRef, outcome: BaseModel) -> str:
        """Validate exact registered outcome identity before durable serialization."""
        entry = self._find(schema)
        if type(outcome) is not entry.outcome_model:
            raise ContractError(("outcome",), "unregistered outcome model")
        validated = entry.outcome_model.model_validate_json(outcome.model_dump_json())
        return canonical_json(validated)

    def validate_event[E: OperationResult | RequestObserved](self, event: E) -> E:
        """Bind typed outcomes to their owning registered wire schema."""
        payload = event.model_dump(mode="python")
        if event.outcome is not None:
            payload["outcome"] = event.outcome
        if isinstance(event, RequestObserved) and event.target is not None:
            target_payload = event.target.model_dump(mode="python")
            if event.target.outcome is not None:
                target_payload["outcome"] = event.target.outcome
            payload["target"] = target_payload
        return type(event).model_validate(payload, context={"operation_registry": self})

    def encode_event(self, event: OperationResult) -> str:
        """Persist an operation callback with its exact registered outcome wire."""
        return canonical_json(self.validate_event(event))

    def decode_event(self, source: str) -> OperationResult:
        """Restore callback subtypes before invoking Strategy.on_event."""
        return OperationResult.model_validate_json(source, context={"operation_registry": self})

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

    @overload
    def validate_decision(self, decision: Operation) -> Operation: ...

    @overload
    def validate_decision(self, decision: Decision) -> Decision: ...

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
        _validate_envelope_version(envelope.schema_version)
        self.validate_core(envelope.core)
        validate_immutable_schema(type(envelope.strategy))
        if not deeply_immutable(envelope):
            raise ContractError(("envelope",), "immutable durable value required")
        return canonical_json(envelope)

    def decode_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], source: str
    ) -> RunEnvelope[S]:
        """Resume only with matching schemas and the same registered codec."""
        _validate_envelope_version(_read_envelope_version(source, ("schema_version",)))
        envelope = model.model_validate_json(source, context={"operation_registry": self})
        if envelope.strategy_id != envelope.core.run.declaration.strategy_id:
            raise ContractError(("strategy_id",), "strategy declaration mismatch")
        if envelope.state_schema != envelope.core.run.declaration.state_schema:
            raise ContractError(("state_schema",), "explicit strategy state migration required")
        if envelope.strategy.schema_version != envelope.state_schema.version:
            raise ContractError(
                ("strategy", "schema_version"), "explicit strategy state migration required"
            )
        self.validate_core(envelope.core)
        validate_immutable_schema(type(envelope.strategy))
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
            if self._find(schema).descriptor != descriptor:
                raise ContractError(("registry", descriptor.kind), "descriptor migration required")
        for intent in state.intents.intents:
            if isinstance(intent.request, ExecuteRegisteredOperation):
                self.decode(intent.request.operation)
                if intent.outcome_json is not None:
                    self.decode_outcome(intent.request.operation.schema_ref, intent.outcome_json)


def operation_result(operation_id: OperationId, event: RequestObserved) -> OperationResult:
    """Let intents forward a codec-validated owner payload to Strategy.on_event."""
    if not event.outcome_is_registered or event.operation_schema is None or event.outcome is None:
        raise ContractError(("outcome",), "registered observation outcome required")
    result = OperationResult(
        operation_id=operation_id,
        observation=event.observation,
        outcome_schema=event.operation_schema.outcome_schema,
        operation_schema=event.operation_schema,
        outcome_json=event.outcome_json,
        outcome=event.outcome,
    )
    return prove_outcome(result, event.outcome, event.operation_schema)


def _read_envelope_version(source: str, path: tuple[str | int, ...]) -> int:
    try:
        header = json.loads(source)
    except json.JSONDecodeError as error:
        raise ContractError(path, "valid JSON object envelope required") from error
    version = header.get("schema_version") if isinstance(header, dict) else None
    if type(version) is not int:
        raise ContractError(path, "integer envelope version required; explicit migration required")
    return version


def _validate_envelope_version(version: object) -> None:
    if type(version) is not int or version != ENVELOPE_SCHEMA_VERSION:
        raise ContractError(("schema_version",), "explicit envelope migration required")


def _normalize[M: BaseModel](
    request: OperationRequest,
    normalizer: Callable[[OperationRequest], M],
    model: type[M],
    path: tuple[str | int, ...],
) -> M:
    """Validate extension results before attaching any durable normalization proof."""
    try:
        normalized = normalizer(request)
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractError(path, str(error)) from error
    if type(normalized) is not model:
        raise ContractError(path, f"normalizer must return exact {model.__name__} value")
    try:
        immutable = deeply_immutable(normalized)
    except AttributeError as error:
        raise ContractError(path, str(error)) from error
    if not immutable:
        raise ContractError(path, "normalizer must return deeply immutable value")
    try:
        return model.model_validate_json(normalized.model_dump_json())
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractError(path, str(error)) from error


def _validate_normalizer(registration: OperationRegistration, index: int) -> None:
    for name, normalizer in (
        ("normalize_turn", registration.normalize_turn),
        ("normalize_scope_reopen", registration.normalize_scope_reopen),
        ("normalize_measurement", registration.normalize_measurement),
    ):
        if normalizer is not None and not callable(normalizer):
            raise ContractError(("registry", index, name), "normalizer must be callable")
    if (
        registration.normalize_measurement is not None
        and registration.descriptor.lifecycle != LifecycleClass.OWNED_JOB
    ):
        raise ContractError(
            ("registry", index, "normalize_measurement"),
            "only owned jobs grant measurement authority",
        )
    if (registration.descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN) != (
        registration.normalize_scope_reopen is not None
    ):
        raise ContractError(
            ("registry", index, "normalize_scope_reopen"),
            "scope reopening requires its declared explicit normalizer",
        )
    if (registration.descriptor.lifecycle == LifecycleClass.SESSION_TURN) != (
        registration.normalize_turn is not None
    ):
        raise ContractError(
            ("registry", index, "normalize_turn"),
            "session turns require an explicit TurnSpec normalizer",
        )


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
