"""Generator rejects registry drift using real descriptors, without protoc I/O."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from hypothesis import given
from hypothesis import strategies as st
from scripts.generate_proto_framing import HOST_API_MAX_MESSAGE_BYTES
from scripts.host_operations.api import (
    GenerationError,
    compile_outputs,
    compile_registry,
    compile_subscribe_timing,
    compile_topics,
)

from vs_proto.api import (
    OperationFailure,
    SubscribeRequest,
    host_operation_descriptors,
    host_operations,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SLOTS = tuple(host_operations.ADAPTER_OPERATIONS)
_STREAM = 2
_HANDLERS = tuple(
    op.name for op in host_operations.OPERATIONS if op.effect == 1 and op.cardinality != _STREAM
)


def _image() -> descriptor_pb2.FileDescriptorSet:
    seen = {}

    def add(file: object) -> None:
        if file.name in seen:
            return
        for imported in file.dependencies:
            add(imported)
        seen[file.name] = descriptor_pb2.FileDescriptorProto.FromString(file.proto.to_binary())

    add(host_operation_descriptors.desc())
    add(OperationFailure.desc().file)
    add(SubscribeRequest.desc().file)
    return descriptor_pb2.FileDescriptorSet(file=list(seen.values()))


def _slots() -> tuple[str, ...]:
    return (
        "list_tasks",
        "list_run_digests",
        "usage_summary",
        "usage_rates_refresh",
        "usage_limits",
        "list_harnesses",
        "list_examples",
        "get_task_goal",
        "describe_machine",
        "list_compute",
        "list_agent_logins",
        "inspect_destination",
        "preflight_run",
        "terminate_run",
        "put_cluster",
        "remove_cluster",
        "test_cluster",
        "create_example",
        "copy_project",
        "start_run",
        "usage_prices_save",
    )


def _watches() -> tuple[str, ...]:
    return ("login", "stream_saved_runs")


def _changed(change: Callable[[object], None], index: int = 0) -> bytes:
    image = _image()
    pool = descriptor_pool.DescriptorPool()
    for file in image.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.host.v1.operation")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.MethodOptions")
    )
    file = next(file for file in image.file if file.name.endswith("host_operations.proto"))
    method = file.service[0].method[index]
    opts = options.FromString(method.options.SerializeToString())
    change(opts.Extensions[extension])
    method.options.ParseFromString(opts.SerializeToString())
    return image.SerializeToString()


def test_every_method_has_one_typed_handler_and_canonical_budget() -> None:
    compiled = compile_registry(_image().SerializeToString(), _slots(), _watches())
    reads = [op for op in compiled if op.is_read()]
    assert [op.name for op in reads if op.policy.executor == 1] == list(_HANDLERS)
    assert [op.name for op in reads if op.policy.executor != 1] == [
        "InspectCandidate",
        "CompletePaths",
    ]
    assert [(op.slot, op.policy.cap_ms) for op in compiled if op.is_host_call()] == [
        ("post_intent", 10000),
        ("relaunch", 30000),
        ("chat_message", 10000),
        ("create_chat_session", 10000),
    ]
    assert [(op.slot, op.policy.cap_ms) for op in compiled if op.is_watch()] == [
        ("login", 600000),
        ("stream_saved_runs", 600000),
    ]
    assert {op.policy.cap_ms + op.policy.grace_ms for op in compiled} == {
        10000,
        15000,
        30000,
        35000,
        70000,
        65000,
        125000,
        605000,
    }


@pytest.mark.parametrize(
    "handlers", [(), _slots()[:-1], (*_slots(), "list_tasks"), (*_slots(), "unknown")]
)
def test_missing_duplicate_and_unknown_handlers_fail_generation(
    handlers: tuple[str, ...],
) -> None:
    with pytest.raises(GenerationError, match="handlers"):
        compile_registry(_image().SerializeToString(), handlers)


@pytest.mark.parametrize("field", ["operation_id", "request_tag", "response_tag"])
def test_duplicate_identifiers_fail_generation(field: str) -> None:
    image = _changed(lambda policy: setattr(policy, field, 10), index=1)
    with pytest.raises(GenerationError, match=f"duplicate {field}"):
        compile_registry(image, _slots())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cap_ms", 0, "budgets"),
        ("grace_ms", 0, "budgets"),
        ("retry", 3, "effect/retry"),
        ("effect", 3, "effect/retry"),
        ("cardinality", 2, "cardinality"),
        ("executor", 0, "executor"),
        ("request_tag", 19000, "request_tag"),
        ("response_tag", 0, "response_tag"),
        ("first_event_ms", 1, "first_event"),
        ("progress_ms", 1, "progress_ms"),
        *(
            (field, retired, "retired")
            for retired in (37, 42)
            for field in ("operation_id", "request_tag", "response_tag")
        ),
    ],
)
def test_invalid_policies_name_the_method(field: str, value: int, message: str) -> None:
    image = _changed(lambda policy: setattr(policy, field, value))
    with pytest.raises(GenerationError, match=f"ListTasks: .*{message}"):
        compile_registry(image, _slots())


@given(number=st.integers(min_value=100, max_value=10000))
def test_every_bad_correlation_path_fails_generation(number: int) -> None:
    def change(policy: object) -> None:
        policy.correlation[0].request_path[:] = [number]

    with pytest.raises(GenerationError, match="correlation"):
        compile_registry(_changed(change), _slots())


def test_a_correlation_cannot_cross_a_scalar_even_with_repeated_field_numbers() -> None:
    def change(policy: object) -> None:
        policy.correlation[0].request_path[:] = [1, 1]

    with pytest.raises(GenerationError, match="correlation crosses a scalar"):
        compile_registry(_changed(change), _slots())


def test_the_descriptor_pass_compiles_all_language_outputs_before_publication() -> None:
    image = _image().SerializeToString()
    outputs = compile_outputs(image, _slots(), _watches())
    assert len(outputs) == 7
    for path, content in outputs.items():
        if path.endswith(".py"):
            ast.parse(content)
    assert compile_outputs(image, _slots(), _watches()) == outputs


def _topic_image(
    change: Callable[[descriptor_pb2.DescriptorProto, descriptor_pb2.DescriptorProto], None],
) -> bytes:
    """Rewrite the policy option of `MachineStatsTopic`, and the file holding `TopicEntry`."""
    image = _image()
    file = next(f for f in image.file if f.name == "vibesys/api/v1/subscribe.proto")
    messages = {message.name: message for message in file.message_type}
    change(messages["MachineStatsTopic"], messages["TopicEntry"])
    return image.SerializeToString()


def test_topic_policies_compile_from_the_descriptor() -> None:
    topics = compile_topics(_image().SerializeToString())
    assert [
        (
            topic.name,
            topic.policy.cadence_ms,
            topic.policy.max_keys,
            topic.policy.source_lease_ms,
        )
        for topic in topics
    ] == [
        ("machine_stats", 10000, 1, 0),
        ("runs", 0, 16384, 30000),
    ]


def test_a_topic_without_a_policy_fails_generation() -> None:
    def change(topic: descriptor_pb2.DescriptorProto, _: object) -> None:
        topic.options.Clear()

    with pytest.raises(GenerationError, match="MachineStatsTopic: missing topic policy"):
        compile_topics(_topic_image(change))


def test_a_topic_without_its_value_arm_fails_generation() -> None:
    def change(_: object, entry: descriptor_pb2.DescriptorProto) -> None:
        for field in entry.field:
            if field.name == "machine_stats":
                field.name = "renamed"

    with pytest.raises(GenerationError, match=r"no TopicEntry\.value arm"):
        compile_topics(_topic_image(change))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("first_frame_ms", 0, "first_frame_ms"),
        ("max_keys", 0, "max_keys"),
        ("max_value_bytes", 0, "max_value_bytes"),
        # A value of exactly the transport cap leaves no room for its key and framing.
        ("max_value_bytes", HOST_API_MAX_MESSAGE_BYTES, "exceeds the transport cap"),
        (
            "max_value_bytes",
            HOST_API_MAX_MESSAGE_BYTES + 1,
            "exceeds the transport cap",
        ),
        ("stale_after_ms", 10000, "stale_after_ms must exceed cadence_ms"),
    ],
)
def test_invalid_topic_policies_name_the_topic(field: str, value: int, message: str) -> None:
    number = {
        "stale_after_ms": 2,
        "first_frame_ms": 3,
        "max_keys": 4,
        "max_value_bytes": 6,
    }[field]

    def change(topic: descriptor_pb2.DescriptorProto, _: object) -> None:
        # A later occurrence of a scalar wins, so appending overrides the checked-in policy.
        raw = topic.options.SerializeToString()
        topic.options.ParseFromString(raw + _append_to_policy(number, value))

    with pytest.raises(GenerationError, match=f"MachineStatsTopic: .*{message}"):
        compile_topics(_topic_image(change))


def test_a_value_with_room_for_its_key_and_framing_is_accepted() -> None:
    def change(topic: descriptor_pb2.DescriptorProto, _: object) -> None:
        raw = topic.options.SerializeToString()
        topic.options.ParseFromString(raw + _append_to_policy(6, HOST_API_MAX_MESSAGE_BYTES - 1024))

    assert (
        compile_topics(_topic_image(change))[0].policy.max_value_bytes
        == HOST_API_MAX_MESSAGE_BYTES - 1024
    )


def _timing_image(change: Callable[[descriptor_pb2.DescriptorProto], None]) -> bytes:
    """Rewrite the frame timing option of `SubscribeRequest`."""
    image = _image()
    file = next(f for f in image.file if f.name == "vibesys/api/v1/subscribe.proto")
    change(next(message for message in file.message_type if message.name == "SubscribeRequest"))
    return image.SerializeToString()


def test_frame_timing_compiles_from_the_descriptor_into_both_languages() -> None:
    image = _image().SerializeToString()
    timing = compile_subscribe_timing(image)
    assert (timing.heartbeat_ms, timing.lease_ms) == (5000, 30000)
    outputs = compile_outputs(image, _slots(), _watches())
    typescript = outputs["clients/libs/proto/src/generated/host-operations.ts"]
    go = outputs["gateway/internal/hostoperations/topics_generated.go"]
    assert f"heartbeatMs: {timing.heartbeat_ms}, leaseMs: {timing.lease_ms}" in typescript
    assert (
        f"FrameTiming{{Heartbeat: {timing.heartbeat_ms} * time.Millisecond, "
        f"Lease: {timing.lease_ms} * time.Millisecond}}"
    ) in go


def test_a_request_without_frame_timing_fails_generation() -> None:
    def change(request: descriptor_pb2.DescriptorProto) -> None:
        request.options.Clear()

    with pytest.raises(GenerationError, match="SubscribeRequest: missing subscribe timing"):
        compile_subscribe_timing(_timing_image(change))


@pytest.mark.parametrize(
    ("heartbeat", "lease", "message"),
    [
        (0, 30000, "heartbeat_ms and lease_ms are required"),
        (5000, 0, "heartbeat_ms and lease_ms are required"),
        (5000, 5000, "lease_ms must exceed heartbeat_ms"),
        (5000, 4999, "lease_ms must exceed heartbeat_ms"),
        (1, 2, None),
    ],
)
def test_a_lease_that_cannot_hold_a_beat_fails_generation(
    heartbeat: int, lease: int, message: str | None
) -> None:
    def change(request: descriptor_pb2.DescriptorProto) -> None:
        update = _varint(1 << 3) + _varint(heartbeat) + _varint(2 << 3) + _varint(lease)
        option = _varint(51005 << 3 | 2) + _varint(len(update)) + update
        request.options.Clear()
        request.options.ParseFromString(option)

    image = _timing_image(change)
    if message is None:
        timing = compile_subscribe_timing(image)
        assert (timing.heartbeat_ms, timing.lease_ms) == (heartbeat, lease)
        return
    with pytest.raises(GenerationError, match=f"SubscribeRequest: {message}"):
        compile_subscribe_timing(image)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append(value & 0x7F | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _append_to_policy(number: int, value: int) -> bytes:
    """Encode a `TopicPolicy` option field, which protobuf merges into the one already set."""
    update = _varint(number << 3) + _varint(value)
    return _varint(51004 << 3 | 2) + _varint(len(update)) + update


def _with_minimum_version(version: str) -> bytes:
    image = _image()
    pool = descriptor_pool.DescriptorPool()
    for file in image.file:
        pool.Add(file)
    extension = pool.FindExtensionByName("vibesys.host.v1.minimum_checkout_version")
    options = message_factory.GetMessageClass(
        pool.FindMessageTypeByName("google.protobuf.FileOptions")
    )
    file = next(file for file in image.file if file.name == "vibesys/host/v1/operations.proto")
    opts = options.FromString(file.options.SerializeToString())
    opts.Extensions[extension] = version
    file.options.ParseFromString(opts.SerializeToString())
    return image.SerializeToString()


@pytest.mark.parametrize("version", ["", "1.2", "1.2.3.4", "01.2.3", "v1.2.3", "1.2.3-dev"])
def test_minimum_version_rejects_missing_or_invalid_policy(version: str) -> None:
    with pytest.raises(GenerationError, match="minimum_checkout_version"):
        compile_outputs(_with_minimum_version(version), _slots(), _watches())
