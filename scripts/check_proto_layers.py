"""Enforce framework, shared-facts and per-strategy message ownership.

Strategy packages live at proto/vibesys/strategies/<owner>/v1 and declare
vibesys.strategies.<owner>.v1. Their generated bindings stay out of the generic
vs_proto.api facade. Core and generic viewers cannot import strategy bindings.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
from pathlib import Path

_STRATEGY_BINDING = re.compile(r"(?:^|[./])strategies(?:[./]|$)")
_STRATEGY = re.compile(r"(?:^|[./])strategies[./]([a-zA-Z0-9_]+)(?:[./]|$)")
_SHARED = ("vibesys/store/", "vibesys/core/")
_EXTERNAL = ("google/protobuf/", "buf/validate/")
_GENERIC = (
    "libs/vs-core/src/",
    "libs/vs-runlog/src/",
    "src/headless/",
    "src/server/",
    "src/vibesys/records/",
    "clients/",
)


def _proto_declarations(source: str) -> tuple[str | None, tuple[str, ...]]:
    # Tokenize strings before keywords: comment delimiters or the word import
    # inside a CEL rule cannot hide or invent a dependency.
    tokens = re.findall(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|[a-zA-Z_][a-zA-Z0-9_]*|[.;]', source, flags=re.DOTALL
    )
    tokens = [token for token in tokens if not token.startswith(("//", "/*"))]
    package = None
    imports = []
    for index, lexeme in enumerate(tokens):
        if lexeme == "package":
            rest = tokens[index + 1 :]
            if ";" in rest:
                package = "".join(rest[: rest.index(";")])
        if lexeme == "import":
            rest = tokens[index + 1 :]
            if rest and rest[0] in {"weak", "public"}:
                rest = rest[1:]
            if len(rest) > 1 and rest[0].startswith('"') and rest[1] == ";":
                imports.append(rest[0][1:-1])
    return package, tuple(imports)


def _proto_violations(path: str, source: str) -> list[str]:
    relative = path.removeprefix("proto/")
    strategy = _STRATEGY.search(relative)
    shared = relative.startswith(_SHARED)
    package, imports = _proto_declarations(source)
    if strategy is None and not shared:
        if package is not None and package.startswith("vibesys.strategies."):
            return [f"{path}: strategy package must live under proto/vibesys/strategies/<owner>/v1"]
        return []
    found = []
    if strategy is not None:
        expected = f"vibesys.strategies.{strategy[1]}.v1"
        if package != expected:
            found.append(f"{path}: strategy messages must declare package {expected}")
    for dependency in imports:
        own = strategy is not None and dependency.startswith(f"vibesys/strategies/{strategy[1]}/")
        if not dependency.startswith((*_EXTERNAL, *_SHARED)) and not own:
            found.append(
                f"{path}: forbidden message dependency {dependency}; import framework/shared facts or the strategy's own package"
            )
    return found


def _python_imports(source: str) -> list[str]:
    imports = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.extend(f"{node.module or ''}.{alias.name}" for alias in node.names)
    return imports


def violations(files: Mapping[str, str]) -> tuple[str, ...]:
    """Check supplied source text without Git, subprocesses or generated-code imports."""
    found = []
    for path, source in sorted(files.items()):
        if path.endswith(".proto"):
            found.extend(_proto_violations(path, source))
        elif path == "libs/vs-proto/src/vs_proto/api.py" or path.startswith(_GENERIC):
            if "/generated/" in path or "/_generated/" in path or "/node_modules/" in path:
                continue
            imports = (
                _python_imports(source)
                if path.endswith(".py")
                else re.findall(
                    r"""(?:from\s*|import\s*\(|require\s*\()\s*["']([^"']+)["']""", source
                )
            )
            found.extend(
                f"{path}: generic core/viewer cannot import strategy messages: {dependency}; use shared facts or retained descriptors"
                for dependency in imports
                if _STRATEGY_BINDING.search(dependency)
            )
    return tuple(found)


def main() -> int:
    """Scan owned source trees, excluding build and dependency directories."""
    root = Path(__file__).resolve().parents[1]
    paths = [*root.glob("proto/vibesys/**/*.proto")]
    paths.append(root / "libs/vs-proto/src/vs_proto/api.py")
    for scope in _GENERIC:
        for directory, dirs, filenames in (root / scope).walk():
            dirs[:] = [
                name
                for name in dirs
                if name not in {"node_modules", "generated", "_generated", "dist", ".venv"}
            ]
            paths.extend(
                directory / name
                for name in filenames
                if Path(name).suffix in {".py", ".ts", ".tsx"}
            )
    files = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
        if not any(
            part in {"node_modules", "generated", "_generated", "dist"} for part in path.parts
        )
    }
    found = violations(files)
    for problem in found:
        print(problem)
    return int(bool(found))


if __name__ == "__main__":
    raise SystemExit(main())
