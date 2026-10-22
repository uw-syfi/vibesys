"""Run the host-operation descriptor pass before normal language generation."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from scripts.host_operations.api import compile_outputs


def generate(root: Path, image: bytes, *, include_schema: bool = True) -> None:
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
    paths = (
        root / "src/server/host_api.py",
        *sorted(
            path for path in (root / "src/server").glob("host_*.py") if path.name != "host_api.py"
        ),
    )
    sources = tuple(path.read_text() for path in paths)
    watches = tuple(
        constant.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "WATCH_SLOTS" for target in node.targets
        )
        and isinstance(node.value, ast.Tuple)
        for constant in node.value.elts
        if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
    )
    outputs = compile_outputs(image, registrations, sources, watches)
    for name, content in outputs.items():
        if name.endswith(".proto") and not include_schema:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    generate(
        Path(__file__).resolve().parents[2],
        Path(sys.argv[1]).read_bytes(),
        include_schema="--bindings" not in sys.argv,
    )
