"""Pure operation schema registration, wire decoding and startup validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args, get_origin, overload

from pydantic import JsonValue, TypeAdapter, ValidationError

from ._envelope_migrations import (
    OLDEST_SUPPORTED_ENVELOPE_VERSION,
    upgrade_envelope_document,
)
from ._outcomes import prove_outcome
from ._values import (
    ImmutableSchemaError,
    InstanceMemo,
    canonical_json,
    deeply_immutable,
    encode_json,
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
from .types.records import OperationResult, RequestObserved
from .types.sessions import TurnSpec
from .types.strategy import Command, Operation, StrategyState, Wait

ENVELOPE_SCHEMA_VERSION = 38
"""The version of the envelope a run writes now.

Raising it needs, in the same change, the migration of the previous version in
``vs_core._envelope_migrations`` and a fixture envelope saved by the previous version's code
(``tests/envelopes``); ``tests/test_envelope_migrations.py`` fails without both. A run saved at
any version from the oldest supported one resumes through that chain.
"""

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from .types.common import Observation, RequestId
    from .types.kernel import CoreState, RunEnvelope
    from .types.strategy import OperationRequest, SearchDeclaration, Submission


class ContractError(ValueError):
    """Strict ingress or startup error naming the offending contract path."""

    def __init__(self, path: tuple[str | int, ...], detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{'.'.join(map(str, path))}: {detail}")


class ClosedRecordContradictionError(ContractError):
    """An executor's answer for a closed request contradicts the disposition core recorded.

    A closed record's status, acceptance and resource never change: its owner has already
    acted on them. An answer that only adds release facts or children is accepted; this one
    claims a different outcome, which is a defect in the executor or in core.
    """

    def __init__(
        self, request_id: RequestId, closed: Observation | None, answer: Observation
    ) -> None:
        self.request_id = request_id
        self.closed = closed
        self.answer = answer
        recorded = "no observation" if closed is None else closed.status.value
        super().__init__(
            ("observation",),
            f"request {request_id.root} closed as {recorded}; "
            f"a later answer of {answer.status.value} contradicts it",
        )


class UnexpectedEvent(ContractError):  # noqa: N818  # LW-220423 [N818]; the name states the situation it reports, as the search API reads `raise UnexpectedEvent(phase, event)`.
    """A search machine received an event its phase has no transition for.

    Every machine ends its ``match`` with this, so a pair nobody planned for stops the run
    with the phase and the event named instead of being ignored.
    """

    def __init__(self, phase: object, event: object) -> None:
        self.phase = type(phase).__name__
        self.event = str(getattr(event, "kind", type(event).__name__))
        super().__init__(("update", self.phase), f"no transition for the event {self.event}")


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


class OperationRegistry:
    """Closed registered codec; changed schemas require explicit migration."""

    def __init__(self, registrations: tuple[OperationRegistration, ...] = ()) -> None:
        self._checked_records: InstanceMemo[bool] = InstanceMemo()
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
    def validate_decision(self, decision: Command) -> Command: ...

    def validate_decision(self, decision: Command) -> Command:
        """The shell validates operation proposals before calling step."""
        if not isinstance(decision, Operation):
            return decision
        self.encode(decision.request)
        return Operation.model_validate_json(
            decision.model_dump_json(), context={"operation_registry": self}
        )

    def validate_submission(self, decision: Submission) -> Submission:
        """Validate the operation a ``Wait`` carries; any other decision passes through."""
        if not isinstance(decision, Wait):
            return decision
        validated = self.validate_decision(decision.request)
        return decision.model_copy(update={"request": validated})

    def validate_envelope(self, envelope: RunEnvelope) -> None:
        """Check what writing the envelope requires, without serializing it.

        A durable write that serializes the envelope itself (such as the runtime
        record, which embeds it) calls this once instead of paying for a throwaway
        canonical encoding.
        """
        if envelope.schema_version != ENVELOPE_SCHEMA_VERSION:
            raise ContractError(
                ("schema_version",),
                f"envelope version {envelope.schema_version!r} is not the version "
                f"{ENVELOPE_SCHEMA_VERSION} a run writes",
            )
        self.validate_core(envelope.core)
        validate_immutable_schema(type(envelope.strategy))
        if not deeply_immutable(envelope):
            raise ContractError(("envelope",), "immutable durable value required")

    def encode_envelope(self, envelope: RunEnvelope) -> str:
        """Write the whole atomic envelope with registered operation subtypes."""
        self.validate_envelope(envelope)
        return canonical_json(envelope)

    def reload_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], envelope: RunEnvelope[S]
    ) -> RunEnvelope[S]:
        """The envelope as a later process reads it: validated, written, and decoded again.

        It is written the way the runtime persists it (`encode_json`, which reuses the text of
        every unchanged part), not in the sorted canonical form of `encode_envelope`, which
        walks and serializes the whole envelope again each time.
        """
        self.validate_envelope(envelope)
        return self.decode_envelope(model, encode_json(envelope).decode())

    def decode_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], source: str
    ) -> RunEnvelope[S]:
        """Resume with matching schemas and the same registered codec.

        An envelope of an older supported version is brought to the current one first
        (`upgrade_envelope`); every other version is refused.
        """
        source = upgrade_envelope(source, ("schema_version",))
        envelope = model.model_validate_json(
            source, context={"operation_registry": self, "persisted_operation": True}
        )
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
        for record in state.records.records:
            if self._checked_records.get(record):
                continue
            if isinstance(record.request, ExecuteRegisteredOperation):
                self.decode(record.request.operation)
                if record.outcome_json is not None:
                    self.decode_outcome(record.request.operation.schema_ref, record.outcome_json)
            if record.model_config.get("frozen"):
                # A record is checked against this registry's codecs, and a frozen one never
                # changes, so each durable write re-proves only the records it adds.
                self._checked_records.put(record, fact=True)


def operation_result(operation_id: OperationId, event: RequestObserved) -> OperationResult:
    """Let records forward a codec-validated owner payload to Strategy.on_event."""
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


_ANY_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
"""Any JSON value; pydantic's parser bounds nesting, so deep input is invalid, not a crash."""


def upgrade_envelope(source: str | bytes, at: tuple[str, ...]) -> str:
    """The stored JSON ``source`` with its envelope at the current version.

    ``at`` is the keys leading to the envelope's ``schema_version`` inside ``source``. An
    envelope of the current version is returned as it is. One of an older supported version
    goes through the migration chain (`vs_core._envelope_migrations`), which the caller then
    validates in full. A version older than the oldest supported one, or newer than this
    VibeSys writes, is refused before anything of its shape is validated, with an error that
    names the version and who wrote it: validating it in full would fail on whichever field
    moved.
    """
    version = _read_envelope_version(source, at, at)
    _validate_envelope_version(version)
    if version == ENVELOPE_SCHEMA_VERSION:
        return source if isinstance(source, str) else source.decode()
    document = _ANY_JSON.validate_json(source)
    holder = _holder(document, at[:-1], at)
    upgraded = upgrade_envelope_document(holder, to=ENVELOPE_SCHEMA_VERSION)
    if len(at) == 1:
        document = upgraded
    else:
        _holder(document, at[:-2], at)[at[-2]] = upgraded
    return _ANY_JSON.dump_json(document).decode()


def _holder(
    document: JsonValue, keys: tuple[str, ...], at: tuple[str, ...]
) -> dict[str, JsonValue]:
    """The object ``keys`` leads to in ``document``; ``at`` names the envelope in the error."""
    found = document
    for key in keys:
        found = found.get(key) if isinstance(found, dict) else None
    if not isinstance(found, dict):
        raise ContractError(at, "envelope object required")
    return found


def _read_envelope_version(
    source: str | bytes, path: tuple[str | int, ...], at: tuple[str, ...] = ("schema_version",)
) -> int:
    try:
        header = _ANY_JSON.validate_json(source)
    except ValidationError as error:
        raise ContractError(path, "valid JSON object envelope required") from error
    version: object = header
    for key in at:
        version = version.get(key) if isinstance(version, dict) else None
    if type(version) is not int:
        raise ContractError(path, "integer envelope version required; explicit migration required")
    return version


def _validate_envelope_version(version: object) -> None:
    if (
        type(version) is int
        and OLDEST_SUPPORTED_ENVELOPE_VERSION <= version <= ENVELOPE_SCHEMA_VERSION
    ):
        return
    older = type(version) is int and version < OLDEST_SUPPORTED_ENVELOPE_VERSION
    origin = (
        "it was written by an older VibeSys than can be resumed; start a new run"
        if older
        else "it was written by a newer VibeSys; resume it with that version"
    )
    raise ContractError(
        ("schema_version",),
        f"envelope version {version!r} is not supported; versions "
        f"{OLDEST_SUPPORTED_ENVELOPE_VERSION} to {ENVELOPE_SCHEMA_VERSION} are: {origin}",
    )


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


def validate_startup(declaration: SearchDeclaration, available: Capabilities) -> Capabilities:
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
    declaration: SearchDeclaration, available: Capabilities
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
