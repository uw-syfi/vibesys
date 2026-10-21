"""Pinned writer encoding for semantic operation inputs, independent of policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from protobuf import (
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescMessage,
    Message,
)
from protovalidate import validate

from vs_proto.api import replay as pb

ENCODING_VERSION = 1
DIGEST_BYTES = 32
EMPTY_CAUSALITY = sha256(b"vibesys.replay.causality.v1").digest()


class ReplayIdentityError(ValueError):
    """An input cannot be represented by the pinned writer encoding."""


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def chain_dependency(frontier: bytes, dependency: bytes) -> bytes:
    """Append one causal observation without retaining an expanding dependency list."""
    if len(frontier) != DIGEST_BYTES or len(dependency) != DIGEST_BYTES:
        message = "causal digests must contain 32 bytes"
        raise ReplayIdentityError(message)
    return sha256(b"causal\x01" + frontier + dependency).digest()


@dataclass(frozen=True)
class SemanticKey:
    """Stable operation and typed scope; positions refer only to recorded outcomes."""

    operation: str
    scope: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate(self.to_proto())

    def to_proto(self) -> pb.OperationKey:
        return pb.OperationKey(
            operation=self.operation,
            scope=[pb.KeyComponent(kind=kind, value=value) for kind, value in self.scope],
        )

    @classmethod
    def from_proto(cls, key: pb.OperationKey) -> SemanticKey:
        validate(key)
        return cls(key.operation, tuple((part.kind, part.value) for part in key.scope))

    def item(self, item: str) -> SemanticKey:
        return SemanticKey(self.operation, (*self.scope, ("item", item)))

    def parent(self, outcome_digest: bytes) -> SemanticKey:
        if len(outcome_digest) != DIGEST_BYTES:
            message = "parent must be a recorded outcome digest"
            raise ReplayIdentityError(message)
        return SemanticKey(self.operation, (*self.scope, ("parent", outcome_digest.hex())))

    def recorded_position(self, outcome_digest: bytes, position: int) -> SemanticKey:
        if isinstance(position, bool) or position < 0:
            message = "recorded position must be a nonnegative integer"
            raise ReplayIdentityError(message)
        parent = self.parent(outcome_digest)
        return SemanticKey(self.operation, (*parent.scope, ("recorded_position", str(position))))

    def sample(self, sample: str) -> SemanticKey:
        return SemanticKey(self.operation, (*self.scope, ("sample", sample)))

    def revision(self, revision: str) -> SemanticKey:
        return SemanticKey(self.operation, (*self.scope, ("revision", revision)))


@dataclass(frozen=True)
class EncodedIdentity:
    """Canonical bytes are authoritative; digest equality alone never permits reuse."""

    canonical: bytes
    dependencies: bytes
    version: int = ENCODING_VERSION

    @property
    def digest(self) -> bytes:
        return sha256(self.canonical).digest()


def _check_schema(desc: DescMessage, seen: set[str]) -> None:
    if desc.type_name in seen:
        return
    seen.add(desc.type_name)
    if desc.type_name == "google.protobuf.Any":
        message = "identity cannot contain opaque google.protobuf.Any"
        raise ReplayIdentityError(message)
    for field in desc.fields:
        value = field.value
        if isinstance(value, DescFieldValueMap):
            message = f"identity {desc.type_name}.{field.name} cannot be a map"
            raise ReplayIdentityError(message)
        if isinstance(value, DescFieldValueList):
            value = value.element
        if isinstance(value, DescFieldValueMessage):
            _check_schema(value.message, seen)


def _normalize_value(value: object) -> object:
    if isinstance(value, Message):
        return _normalize(value)
    if isinstance(value, float):
        if not isfinite(value):
            message = "identity contains a nonfinite value"
            raise ReplayIdentityError(message)
        return 0.0 if value == 0.0 else value
    return value


def _sort_key(value: object) -> bytes:
    if isinstance(value, Message):
        return value.to_binary()
    return repr(value).encode()


def _normalize(message: Message) -> Message:
    if message.to_binary() != message.to_binary(write_unknown_fields=False):
        problem = f"identity {message.desc().type_name} contains unknown fields"
        raise ReplayIdentityError(problem)
    result = deepcopy(message)
    for field in message:
        value = message[field]
        if isinstance(field.value, DescFieldValueList):
            values = [_normalize_value(item) for item in value]
            options = field.proto.options
            if options is not None and options[pb.ext_replay_set]:
                values = list({_sort_key(item): item for item in values}.values())
                values.sort(key=_sort_key)
            result[field] = values
        else:
            result[field] = _normalize_value(value)
    return result


def encode_identity(
    message: Message, *, contract: str, dependencies: bytes = EMPTY_CAUSALITY
) -> EncodedIdentity:
    """Validate, normalize and encode one identity. Owners never supply encoders.

    Encoding v1 pins protobuf-py 0.6.0 writer order, framed type/contract and
    a fixed-size chained causal digest. It is not cross-language canonicalization.
    """
    if not contract or len(dependencies) != DIGEST_BYTES:
        problem = "identity needs a semantic contract and 32-byte dependencies"
        raise ReplayIdentityError(problem)
    _check_schema(message.desc(), set())
    normalized = _normalize(message)
    validate(normalized)
    canonical = (
        b"vibesys.identity\x01"
        + _frame(contract.encode())
        + _frame(message.desc().type_name.encode())
        + dependencies
        + _frame(normalized.to_binary())
    )
    return EncodedIdentity(canonical, dependencies)


@dataclass(frozen=True)
class ClassifiedContext:
    """Renderer input with every field classified; policy never enters identity."""

    message_type: type[Message]
    content: bytes
    identity: EncodedIdentity

    def message(self) -> Message:
        return self.message_type.from_binary(self.content)


def _check_classification(desc: DescMessage, seen: set[str]) -> None:
    if desc.type_name in seen:
        return
    seen.add(desc.type_name)
    for field in desc.fields:
        options = field.proto.options
        kind = options[pb.ext_replay_field] if options is not None else 0
        if kind not in (pb.ReplayFieldKind.IDENTITY, pb.ReplayFieldKind.POLICY):
            problem = f"unclassified prompt field {desc.type_name}.{field.name}"
            raise ReplayIdentityError(problem)
        value = field.value
        if isinstance(value, DescFieldValueList):
            value = value.element
        if isinstance(value, DescFieldValueMessage):
            _check_classification(value.message, seen)


def _classified(message: Message) -> Message:
    identity = deepcopy(message)
    for field in message.desc().fields:
        options = field.proto.options
        kind = options[pb.ext_replay_field] if options is not None else 0
        if kind not in (pb.ReplayFieldKind.IDENTITY, pb.ReplayFieldKind.POLICY):
            problem = f"unclassified prompt field {message.desc().type_name}.{field.name}"
            raise ReplayIdentityError(problem)
        if kind == pb.ReplayFieldKind.POLICY:
            del identity[field]
        elif field in message:
            value = message[field]
            if isinstance(value, Message):
                identity[field] = _classified(value)
            elif isinstance(field.value, DescFieldValueList) and isinstance(
                field.value.element, DescFieldValueMessage
            ):
                identity[field] = [_classified(item) for item in value]
    return identity


def classify_context(
    message: Message, *, contract: str, dependencies: bytes = EMPTY_CAUSALITY
) -> ClassifiedContext:
    """Reject unclassified renderer inputs, and derive identity from typed content."""
    _check_schema(message.desc(), set())
    _check_classification(message.desc(), set())
    message = _normalize(message)
    validate(message)
    identity = encode_identity(_classified(message), contract=contract, dependencies=dependencies)
    return ClassifiedContext(type(message), message.to_binary(), identity)
