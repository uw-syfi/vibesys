"""Run the host-operation descriptor pass before normal language generation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

from scripts.host_operations._emit import (
    go_registry,
    helper_types,
    python_registry,
    receipt_schema,
    typescript_registry,
)
from scripts.host_operations.api import compile_registry


def generate(root: Path, image: bytes) -> None:
    """Check static slots, then publish all projections from the same descriptor image."""
    tree = ast.parse((root / "src/vibesys/api/host_operations/_handlers.py").read_text())
    registrations = tuple(
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Handlers"
        for keyword in node.keywords
        if keyword.arg is not None
    )
    operations = compile_registry(image, registrations)
    files = descriptor_pb2.FileDescriptorSet.FromString(image)
    failure_file = next(file for file in files.file if file.name == "vibesys/host/v1/failure.proto")
    failures = tuple(
        value.name.removeprefix("FAILURE_CATEGORY_")
        for value in failure_file.enum_type[0].value
        if value.number
    )
    outputs = {
        "src/server/host_operation_types_generated.py": helper_types(root, operations),
        "proto/vibesys/host/v1/admission_receipt_generated.proto": receipt_schema(operations),
        "libs/vs-proto/src/vs_proto/_generated/host_operations.py": python_registry(
            operations, failures
        ),
        "gateway/internal/hostoperations/registry_generated.go": go_registry(operations, failures),
        "clients/libs/proto/src/generated/host-operations.ts": typescript_registry(
            operations, failures
        ),
    }
    for name, content in outputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[2], Path(sys.argv[1]).read_bytes())
