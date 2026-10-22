"""Compile the closed host registry from a Buf descriptor image.

This pass needs no generated Python modules during bootstrap. Invalid metadata
fails generation, before any language-specific output is published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from scripts.host_operations._emit import (
    go_host_call,
    go_registry,
    helper_types,
    python_registry,
    receipt_schema,
    typescript_registry,
)

_RESERVED_TAG_START = 19000
_RESERVED_TAG_END = 19999
_MAX_CAP_MS = 600000
_MAX_GRACE_MS = 30000
_READ = 1
_ADMISSION = 3
_RECONCILE = 3
_BOUNDED_READ = 2
_STREAM = 2
_FILESYSTEM = 3
_FAILURE_RESPONSE_TAG = 2


class GenerationError(ValueError):
    """A method cannot be represented safely by the generated registry."""


@dataclass(frozen=True)
class Operation:
    """One fully checked descriptor projection shared by all emitters."""

    name: str
    request: str
    response: str
    policy: object
    correlations: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]

    @property
    def slot(self) -> str:
        """The statically registered handler slot."""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", self.name).lower()

    def is_read(self) -> bool:
        """Catalog reads use the helper handler slots and the neutral receipt union."""
        return self.policy.effect == _READ

    def is_admission(self) -> bool:
        """HostCall methods keep their API messages and share one generated dispatch table."""
        return self.policy.effect == _ADMISSION


def _fail(path: str, message: str) -> None:
    detail = f"{path}: {message}"
    raise GenerationError(detail)


def _path(message: object, numbers: object, location: str) -> tuple[tuple[str, ...], object]:
    names = []
    field = None
    for index, number in enumerate(numbers):
        field = message.fields_by_number.get(number)
        if field is None or field.is_repeated:
            _fail(location, f"bad correlation field path at {number}")
        names.append(field.name)
        message = field.message_type
        if message is None and index != len(numbers) - 1:
            _fail(location, "correlation crosses a scalar")
    if field is None or field.message_type is not None:
        _fail(location, "correlation must end at a scalar")
    return tuple(names), (field.type, field.enum_type and field.enum_type.full_name)


def _policy(policy: object, method: object) -> None:
    name = method.name
    if not 0 < policy.operation_id <= 2**32 - 1:
        _fail(name, "operation_id must be positive")
    for field in ("request_tag", "response_tag"):
        value = getattr(policy, field)
        if field == "response_tag" and value == _FAILURE_RESPONSE_TAG:
            _fail(name, "response_tag is reserved for typed failure")
        if not 0 < value < 2**29 or _RESERVED_TAG_START <= value <= _RESERVED_TAG_END:
            _fail(name, f"invalid {field}: {value}")
    if not 0 < policy.cap_ms <= _MAX_CAP_MS or not 0 < policy.grace_ms <= _MAX_GRACE_MS:
        _fail(name, "cap_ms/grace_ms must have bounded positive budgets")
    if policy.effect not in (1, 2, 3) or policy.retry not in (1, 2, 3):
        _fail(name, "unknown effect/retry")
    if (policy.effect == _READ and policy.retry == _RECONCILE) or (
        policy.effect != _READ and policy.retry == _BOUNDED_READ
    ):
        _fail(name, "invalid effect/retry combination")
    if policy.effect == _ADMISSION and policy.retry != _RECONCILE:
        _fail(name, "admission requires RECONCILE_SAME_ID")
    if policy.executor not in (1, 2, 3):
        _fail(name, "unknown executor")
    _stream_policy(policy, method)


def _stream_policy(policy: object, method: object) -> None:
    name = method.name
    cardinality = _STREAM if method.server_streaming else 1
    if method.client_streaming or policy.cardinality != cardinality:
        _fail(name, "cardinality disagrees with stream descriptor")
    if policy.HasField("first_event_ms") and (
        cardinality != _STREAM or not 0 < policy.first_event_ms <= policy.cap_ms
    ):
        _fail(name, "invalid first_event_ms")


def compile_registry(image: bytes, handlers: tuple[str, ...]) -> tuple[Operation, ...]:
    """Validate metadata, correlation paths and complete static handler registration."""
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.host.v1.operation")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.MethodOptions")
    )
    service = pool.FindServiceByName("vibesys.api.v1.HostOperationsService")
    operations = []
    seen: dict[str, set[int]] = {
        key: set() for key in ("operation_id", "request_tag", "response_tag")
    }
    for method in service.methods:
        opts = options.FromString(method.GetOptions().SerializeToString())
        if not opts.HasExtension(extension):
            _fail(method.name, "missing operation policy")
        policy = opts.Extensions[extension]
        _policy(policy, method)
        for key, values in seen.items():
            value = getattr(policy, key)
            if value in values:
                _fail(method.name, f"duplicate {key}: {value}")
            values.add(value)
        correlations = []
        for pair in policy.correlation:
            left, left_type = _path(method.input_type, pair.request_path, method.name)
            right, right_type = _path(method.output_type, pair.response_path, method.name)
            if left_type != right_type:
                _fail(method.name, "correlation paths have incompatible types")
            correlations.append((left, right))
        operations.append(
            Operation(
                method.name,
                method.input_type.name,
                method.output_type.name,
                policy,
                tuple(correlations),
            )
        )
    expected = {op.slot for op in operations if op.is_read() and op.policy.executor != _FILESYSTEM}
    if len(set(handlers)) != len(handlers) or set(handlers) != expected:
        _fail(
            service.full_name,
            f"missing, duplicate or unknown handlers: expected {sorted(expected)}, got {sorted(handlers)}",
        )
    return tuple(operations)


def compile_outputs(
    image: bytes, handlers: tuple[str, ...], helper_sources: tuple[str, ...]
) -> dict[str, str]:
    """Compile every projection atomically in memory after validating the complete registry."""
    operations = compile_registry(image, handlers)
    reads = tuple(op for op in operations if op.is_read())
    admissions = tuple(op for op in operations if op.is_admission())
    if len(reads) + len(admissions) != len(operations):
        _fail("HostOperationsService", "operation effect is neither a read nor a host call")
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    failure_file = next(file for file in files.file if file.name == "vibesys/host/v1/failure.proto")
    failures = tuple(
        value.name.removeprefix("FAILURE_CATEGORY_")
        for value in failure_file.enum_type[0].value
        if value.number
    )
    return {
        "src/server/host_operation_types_generated.py": helper_types(helper_sources, reads),
        "proto/vibesys/host/v1/admission_receipt_generated.proto": receipt_schema(reads),
        "libs/vs-proto/src/vs_proto/_generated/host_operations.py": python_registry(
            reads, admissions, failures
        ),
        "gateway/internal/hostoperations/registry_generated.go": go_registry(reads, failures),
        "gateway/internal/hostoperations/host_call_generated.go": go_host_call(admissions),
        "clients/libs/proto/src/generated/host-operations.ts": typescript_registry(
            reads, failures, admissions
        ),
    }
