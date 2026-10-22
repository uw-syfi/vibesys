"""Generator rejects registry drift using real descriptors, without protoc I/O."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from hypothesis import given
from hypothesis import strategies as st
from scripts.host_operations.api import GenerationError, compile_outputs, compile_registry

from vs_proto.api import OperationFailure, host_operation_descriptors, host_operations

if TYPE_CHECKING:
    from collections.abc import Callable

_SLOTS = tuple(host_operations.ADAPTER_OPERATIONS)
_HANDLERS = tuple(op.name for op in host_operations.OPERATIONS)


def _image() -> descriptor_pb2.FileDescriptorSet:
    seen = {}

    def add(file: object) -> None:
        for imported in file.dependencies:
            add(imported)
        seen[file.name] = descriptor_pb2.FileDescriptorProto.FromString(file.proto.to_binary())

    add(host_operation_descriptors.desc())
    add(OperationFailure.desc().file)
    return descriptor_pb2.FileDescriptorSet(file=list(seen.values()))


def _slots() -> tuple[str, ...]:
    return (
        "list_tasks",
        "list_harnesses",
        "list_examples",
        "get_task_goal",
        "describe_machine",
        "list_compute",
        "preflight_run",
    )


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
    compiled = compile_registry(_image().SerializeToString(), _slots())
    assert [op.name for op in compiled if op.is_read()] == list(_HANDLERS)
    assert [(op.slot, op.policy.cap_ms) for op in compiled if op.is_admission()] == [
        ("post_intent", 10000),
        ("relaunch", 30000),
        ("chat_message", 10000),
    ]
    assert {op.policy.cap_ms + op.policy.grace_ms for op in compiled} == {15000, 30000, 35000, 70000}


@pytest.mark.parametrize(
    "handlers", [(), _slots()[:-1], (*_slots(), "list_tasks"), (*_slots(), "unknown")]
)
def test_missing_duplicate_and_unknown_handlers_fail_generation(handlers: tuple[str, ...]) -> None:
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
    root = Path(__file__).resolve().parents[3]
    sources = tuple(
        path.read_text()
        for path in (
            root / "src/server/host_api.py",
            *sorted(
                path
                for path in (root / "src/server").glob("host_*.py")
                if path.name != "host_api.py"
            ),
        )
    )
    image = _image().SerializeToString()
    outputs = compile_outputs(image, _slots(), sources)
    assert len(outputs) == 6
    for path, content in outputs.items():
        if path.endswith(".py"):
            ast.parse(content)
    assert compile_outputs(image, _slots(), sources) == outputs
