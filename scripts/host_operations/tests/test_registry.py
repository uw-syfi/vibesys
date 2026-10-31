"""Generator rejects registry drift using real descriptors, without protoc I/O."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.host_operations.api import GenerationError, compile_outputs, compile_registry
from scripts.host_operations.generate import generate

from vs_proto.api import OperationFailure, host_operation_descriptors, host_operations

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
    return descriptor_pb2.FileDescriptorSet(file=list(seen.values()))


def _slots() -> tuple[str, ...]:
    return (
        "list_tasks",
        "list_run_digests",
        "usage_window",
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
        ("progress_ms", 1, "progress_ms"),
        ("operation_id", 37, "retired"),
        ("request_tag", 37, "retired"),
        ("response_tag", 37, "retired"),
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


def _server_sources(root: Path) -> tuple[str, ...]:
    return tuple(
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


def test_the_descriptor_pass_compiles_all_language_outputs_before_publication() -> None:
    root = Path(__file__).resolve().parents[3]
    image = _image().SerializeToString()
    outputs = compile_outputs(image, _slots(), _server_sources(root), _watches())
    assert len(outputs) == 13
    for path, content in outputs.items():
        if path.endswith(".py"):
            ast.parse(content)
    assert compile_outputs(image, _slots(), _server_sources(root), _watches()) == outputs


def test_every_client_budget_exceeds_its_runner_cap() -> None:
    root = Path(__file__).resolve().parents[3]
    image = _image().SerializeToString()
    outputs = compile_outputs(image, _slots(), _server_sources(root), _watches())
    typescript = outputs["clients/libs/proto/src/generated/host-operations.ts"]
    runner = outputs["gateway/internal/hostoperations/registry_generated.go"]
    calls = outputs["gateway/internal/hostoperations/host_call_generated.go"]
    python = outputs["libs/vs-proto/src/vs_proto/_generated/host_operations.py"]
    compiled = compile_registry(image, _slots(), _watches())
    assert compiled
    for op in compiled:
        client = op.policy.cap_ms + op.policy.grace_ms
        runner_cap = op.policy.cap_ms
        assert client > runner_cap
        if op.is_host_call():
            case = op.slot.split("_", maxsplit=1)[0] + "".join(
                part.title() for part in op.slot.split("_")[1:]
            )
            assert (
                f"{case}: Object.freeze({{capMs: {runner_cap}, graceMs: {op.policy.grace_ms}, totalMs: {client}}}"
                in typescript
            )
            assert f"Deadline: {runner_cap} * time.Millisecond" in calls
            assert (
                f"HostCall({op.slot!r}, {op.slot.replace('_', '-')!r}, "
                f"{op.policy.request_tag}, {op.policy.response_tag}, {runner_cap}, {op.policy.grace_ms})"
                in python
            )
        else:
            case = op.slot.split("_", maxsplit=1)[0] + "".join(
                part.title() for part in op.slot.split("_")[1:]
            )
            assert f"totalMs: {client}" in typescript
            first = (
                f", firstEventMs: {op.policy.first_event_ms if op.policy.HasField('first_event_ms') else 0}"
                f", progressMs: {op.policy.progress_ms if op.policy.HasField('progress_ms') else 0}"
                if op.is_watch()
                else ""
            )
            assert (
                f"{case}: Object.freeze({{capMs: {runner_cap}, graceMs: {op.policy.grace_ms}, totalMs: {client}{first}}}"
                in typescript
            )
            assert f"Cap: {runner_cap}*time.Millisecond" in runner
            assert f"Grace: {op.policy.grace_ms}*time.Millisecond" in runner
            dispatch = (
                "host_watch_generated.go"
                if op.is_watch()
                else "host_mutate_generated.go"
                if op.is_mutation()
                else "host_read_generated.go"
            )
            assert (
                f"Deadline: {runner_cap} * time.Millisecond"
                in outputs[f"gateway/internal/hostoperations/{dispatch}"]
            )


def test_generate_publishes_every_projection(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    handler = root / "src/vibesys/api/host_operations/_handlers.py"
    destination = tmp_path / handler.relative_to(root)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(handler.read_bytes())
    for path in (
        root / "src/server/host_api.py",
        *sorted(
            path for path in (root / "src/server").glob("host_*.py") if path.name != "host_api.py"
        ),
    ):
        target = tmp_path / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    image = _image().SerializeToString()
    expected = compile_outputs(image, _slots(), _server_sources(root), _watches())
    generate(tmp_path, image, include_schema=False)
    for name, content in expected.items():
        published = tmp_path / name
        if name.endswith(".proto"):
            assert not published.exists()
        else:
            assert published.read_text(encoding="utf-8") == content
    generate(tmp_path, image)
    for name, content in expected.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content


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


@given(st.tuples(*(st.integers(min_value=0, max_value=1000) for _ in range(3))))
@example((0, 1, 1))
def test_minimum_version_has_one_owner_for_every_language(release: tuple[int, int, int]) -> None:
    root = Path(__file__).resolve().parents[3]
    version = ".".join(map(str, release))
    outputs = compile_outputs(
        _with_minimum_version(version), _slots(), _server_sources(root), _watches()
    )
    for path in (
        "src/server/host_compatibility_generated.py",
        "libs/vs-proto/src/vs_proto/_generated/host_operations.py",
    ):
        tree = ast.parse(outputs[path])
        values = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            if any(
                isinstance(target, ast.Name) and target.id == "MIN_CHECKOUT_VERSION"
                for target in node.targets
            )
        ]
        assert values == [version]
    assert (
        f'const MinCheckoutVersion = "{version}"'
        in outputs["gateway/internal/hostoperations/registry_generated.go"]
    )


@pytest.mark.parametrize("version", ["", "1.2", "1.2.3.4", "01.2.3", "v1.2.3", "1.2.3-dev"])
def test_minimum_version_rejects_missing_or_invalid_policy(version: str) -> None:
    root = Path(__file__).resolve().parents[3]
    with pytest.raises(GenerationError, match="minimum_checkout_version"):
        compile_outputs(_with_minimum_version(version), _slots(), _server_sources(root), _watches())


def test_native_reads_keep_typed_routing_without_a_legacy_helper_adapter() -> None:
    root = Path(__file__).resolve().parents[3]
    outputs = compile_outputs(
        _image().SerializeToString(), _slots(), _server_sources(root), _watches()
    )
    operations = compile_registry(_image().SerializeToString(), _slots(), _watches())
    native = next(op for op in operations if op.name == "ListRunDigests")
    assert native.policy.adapter_method == ""
    assert "list_run_digests" in outputs["proto/vibesys/api/v1/host_read_generated.proto"]
    assert "ListRunDigests" in outputs["clients/libs/proto/src/generated/host-operations.ts"]
    assert "ListRunDigests" not in outputs["src/server/host_operation_types_generated.py"]
    assert (
        "'': ListRunDigests"
        not in outputs["libs/vs-proto/src/vs_proto/_generated/host_operations.py"]
    )
