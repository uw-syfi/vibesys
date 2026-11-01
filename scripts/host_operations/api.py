"""Compile the closed host registry from a Buf descriptor image.

This pass needs no generated Python modules during bootstrap. Invalid metadata
fails generation, before any language-specific output is published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from scripts.generate_proto_framing import HOST_API_MAX_MESSAGE_BYTES
from scripts.host_operations._emit import (
    Topic,
    go_host_call,
    go_host_mutate,
    go_host_read,
    go_host_watch,
    go_registry,
    go_topics,
    helper_types,
    mutate_envelope,
    python_registry,
    read_envelope,
    receipt_schema,
    typescript_registry,
    typescript_topics,
    watch_envelope,
)

_RESERVED_TAG_START = 19000
_RESERVED_TAG_END = 19999
_MAX_CAP_MS = 600000
_MAX_GRACE_MS = 30000
_READ = 1
_MUTATION = 2
_ADMISSION = 3
_RECONCILE = 3
_BOUNDED_READ = 2
_STREAM = 2
_CHECKOUT = 1
_FAILURE_RESPONSE_TAG = 2
_RETIRED_OPERATIONS = frozenset({37, 42})
"""Ids and tags of removed operations: the paged ``ListSavedRuns`` and ``UsageWindow``. A new use
would misroute a peer."""
# Tags and length prefixes of the response, the entry, its key and its value.
_ENTRY_FRAMING_BYTES = 16


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
    host_call: bool

    @property
    def slot(self) -> str:
        """The statically registered handler slot."""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", self.name).lower()

    def is_read(self) -> bool:
        """Catalog reads use the helper handler slots and the neutral receipt union."""
        return self.policy.effect == _READ and self.policy.cardinality != _STREAM

    def is_host_call(self) -> bool:
        """The HostCall methods keep their API messages and envelope."""
        return self.host_call

    def is_admission(self) -> bool:
        """Durable admission, whether it rides HostCall or a neutral envelope."""
        return self.policy.effect == _ADMISSION

    def is_mutation(self) -> bool:
        """Neutral unary writes. HostCall admissions keep their own envelope."""
        unary = self.policy.cardinality != _STREAM
        return unary and (
            self.policy.effect == _MUTATION or (self.is_admission() and not self.is_host_call())
        )

    def is_watch(self) -> bool:
        """A server stream. The client keeps it open independently of one panel."""
        return self.policy.cardinality == _STREAM


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


def _not_retired(policy: object, name: str) -> None:
    for field in ("operation_id", "request_tag", "response_tag"):
        value = getattr(policy, field)
        if value in _RETIRED_OPERATIONS:
            _fail(name, f"{field} {value} is retired")


def _policy(policy: object, method: object) -> None:
    name = method.name
    if not 0 < policy.operation_id <= 2**32 - 1:
        _fail(name, "operation_id must be positive")
    _not_retired(policy, name)
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
    if policy.HasField("progress_ms") and (
        cardinality != _STREAM or not 0 < policy.progress_ms <= policy.cap_ms
    ):
        _fail(name, "invalid progress_ms")


def compile_registry(
    image: bytes, handlers: tuple[str, ...], watches: tuple[str, ...] = ()
) -> tuple[Operation, ...]:
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
    host_call_requests = {
        field.message_type.full_name
        for field in pool.FindMessageTypeByName("vibesys.api.v1.HostCallRequest")
        .oneofs_by_name["method"]
        .fields
    }
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
                method.input_type.full_name in host_call_requests,
            )
        )
    expected = {op.slot for op in operations if op.is_read() and op.policy.executor == _CHECKOUT}
    expected |= {op.slot for op in operations if op.is_mutation()}
    if len(set(handlers)) != len(handlers) or set(handlers) != expected:
        _fail(
            service.full_name,
            f"missing, duplicate or unknown handlers: expected {sorted(expected)}, got {sorted(handlers)}",
        )
    expected_watches = {op.slot for op in operations if op.is_watch()}
    if len(set(watches)) != len(watches) or set(watches) != expected_watches:
        _fail(
            service.full_name,
            f"missing, duplicate or unknown watches: expected {sorted(expected_watches)}, got {sorted(watches)}",
        )
    return tuple(operations)


def compile_subscribe_timing(image: bytes) -> object:
    """Read `SubscribeRequest`'s frame timing, failing when it is missing or the lease cannot hold a beat."""
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.api.v1.subscribe_timing")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.MessageOptions")
    )
    request = pool.FindMessageTypeByName("vibesys.api.v1.SubscribeRequest")
    opts = options.FromString(request.GetOptions().SerializeToString())
    if not opts.HasExtension(extension):
        _fail(request.name, "missing subscribe timing")
    timing = opts.Extensions[extension]
    if not (timing.heartbeat_ms and timing.lease_ms):
        _fail(request.name, "heartbeat_ms and lease_ms are required")
    if timing.lease_ms <= timing.heartbeat_ms:
        _fail(request.name, "lease_ms must exceed heartbeat_ms")
    return timing


def compile_topics(image: bytes) -> tuple[Topic, ...]:
    """Read each `SubscribeRequest.topic` arm's policy, failing on one with none or a bad one."""
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.api.v1.topic")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.MessageOptions")
    )
    request = pool.FindMessageTypeByName("vibesys.api.v1.SubscribeRequest")
    value_room = HOST_API_MAX_MESSAGE_BYTES - _ENTRY_FRAMING_BYTES - _entry_key_bytes(pool)
    values = {
        field.name: field.message_type.full_name
        for field in pool.FindMessageTypeByName("vibesys.api.v1.TopicEntry")
        .oneofs_by_name["value"]
        .fields
    }
    topics = []
    for field in request.oneofs_by_name["topic"].fields:
        opts = options.FromString(field.message_type.GetOptions().SerializeToString())
        if not opts.HasExtension(extension):
            _fail(field.message_type.name, "missing topic policy")
        policy = opts.Extensions[extension]
        if field.name not in values:
            _fail(field.message_type.name, "no TopicEntry.value arm of the same name")
        if not (policy.first_frame_ms and policy.max_keys and policy.max_value_bytes):
            _fail(field.message_type.name, "first_frame_ms, max_keys, max_value_bytes are required")
        if policy.max_value_bytes > value_room:
            _fail(field.message_type.name, "max_value_bytes exceeds the transport cap")
        if policy.stale_after_ms and policy.stale_after_ms <= policy.cadence_ms:
            _fail(field.message_type.name, "stale_after_ms must exceed cadence_ms")
        topics.append(Topic(field.name, policy))
    if len(topics) != len(values):
        _fail("TopicEntry", "a value arm has no SubscribeRequest.topic arm of the same name")
    return tuple(topics)


def _entry_key_bytes(pool: descriptor_pool.DescriptorPool) -> int:
    """The longest key an entry may carry, as `TopicEntry.key` states it."""
    rules = pool.FindExtensionByName("buf.validate.field")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.FieldOptions")
    )
    key = pool.FindMessageTypeByName("vibesys.api.v1.TopicEntry").fields_by_name["key"]
    return (
        options.FromString(key.GetOptions().SerializeToString()).Extensions[rules].string.max_bytes
    )


def _minimum_checkout_version(image: bytes) -> str:
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.host.v1.minimum_checkout_version")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.FileOptions")
    )
    path = "vibesys/host/v1/operations.proto"
    opts = options.FromString(pool.FindFileByName(path).GetOptions().SerializeToString())
    version = opts.Extensions[extension]
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        _fail(path, "minimum_checkout_version must be a major.minor.patch release")
    return version


def compile_outputs(
    image: bytes,
    handlers: tuple[str, ...],
    helper_sources: tuple[str, ...],
    watches: tuple[str, ...] = (),
) -> dict[str, str]:
    """Compile every projection atomically in memory after validating the complete registry."""
    operations = compile_registry(image, handlers, watches)
    minimum = _minimum_checkout_version(image)
    reads = tuple(op for op in operations if op.is_read())
    checkout_reads = tuple(op for op in reads if op.policy.executor == _CHECKOUT)
    mutations = tuple(op for op in operations if op.is_mutation())
    watch_ops = tuple(op for op in operations if op.is_watch())
    host_calls = tuple(op for op in operations if op.is_host_call())
    if len(reads) + len(mutations) + len(watch_ops) + len(host_calls) != len(operations):
        _fail(
            "HostOperationsService",
            "operation effect is neither a read, mutation, watch, or host call",
        )
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    failure_file = next(file for file in files.file if file.name == "vibesys/host/v1/failure.proto")
    failures = tuple(
        value.name.removeprefix("FAILURE_CATEGORY_")
        for value in failure_file.enum_type[0].value
        if value.number
    )
    neutral = reads + mutations + watch_ops
    topics = compile_topics(image)
    timing = compile_subscribe_timing(image)
    outputs = {
        "src/server/host_compatibility_generated.py": (
            '"""Generated by scripts/generate_proto.sh; do not edit."""\n\n'
            f"MIN_CHECKOUT_VERSION = {minimum!r}\n"
            '"""The oldest VibeSys version a checkout may have for this app to use it."""\n'
        ),
        "src/server/host_operation_types_generated.py": helper_types(
            helper_sources, checkout_reads
        ),
        "proto/vibesys/host/v1/admission_receipt_generated.proto": receipt_schema(neutral),
        "libs/vs-proto/src/vs_proto/_generated/host_operations.py": python_registry(
            checkout_reads, host_calls, failures, mutations, watch_ops
        )
        + f"\nMIN_CHECKOUT_VERSION = {minimum!r}\n",
        "gateway/internal/hostoperations/registry_generated.go": go_registry(
            neutral, failures, minimum
        ),
        "gateway/internal/hostoperations/host_call_generated.go": go_host_call(host_calls),
        "gateway/internal/hostoperations/host_read_generated.go": go_host_read(reads),
        "proto/vibesys/api/v1/host_read_generated.proto": read_envelope(reads),
        "clients/libs/proto/src/generated/host-operations.ts": typescript_registry(
            reads, failures, host_calls, mutations, watch_ops
        )
        + typescript_topics(topics, timing),
        "gateway/internal/hostoperations/topics_generated.go": go_topics(topics, timing),
    }
    if mutations:
        outputs["proto/vibesys/api/v1/host_mutate_generated.proto"] = mutate_envelope(mutations)
        outputs["gateway/internal/hostoperations/host_mutate_generated.go"] = go_host_mutate(
            mutations
        )
    if watch_ops:
        outputs["proto/vibesys/api/v1/host_watch_generated.proto"] = watch_envelope(watch_ops)
        outputs["gateway/internal/hostoperations/host_watch_generated.go"] = go_host_watch(
            watch_ops
        )
    return outputs
