"""Static scan for disallowed serving-engine code in a candidate workspace.

The bundle forbids importing, copying, vendoring, or pip-installing the
sglang, vllm, and TensorRT-LLM engines (see OBJECTIVE.md, "Disallowed engine
code"). This scan is the mechanical part of that rule: it flags engine imports,
install commands, and vendored engine directories. It cannot prove a candidate
clean (copied code can be renamed), so the judge still reviews the source.

Usage: python3 engine_scan.py [--workspace DIR]   (exit 1 when findings exist)
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

DISALLOWED_MODULES = frozenset(
    {"sglang", "sgl_kernel", "sgl_router", "sglang_router", "vllm", "tensorrt_llm"}
)
_ENGINE_NAMES = r"sglang|sgl[-_]kernel|sgl[-_]router|vllm|tensorrt[-_]llm"
_INSTALL = re.compile(rf"(pip3?|uv\s+pip|uv\s+add)\s+(install\s+)?[^\n]*\b({_ENGINE_NAMES})\b")
_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".vibesys", "progress"}
)
# Harness-owned directories legitimately name the engines.
_HARNESS_DIRS = frozenset({"accuracy_checker", "benchmark", "reference"})
_TEXT_SUFFIXES = frozenset({".sh", ".txt", ".toml", ".cfg", ".yaml", ".yml", ".sbatch"})


def _files(root: Path):  # noqa: ANN202
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in parts) or parts[0] in _HARNESS_DIRS:
            continue
        if path.is_file():
            yield path


def _module_findings(path: Path, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"[A-Za-z_][\w.]*", node.value)
        ):
            names = [node.value]  # importlib.import_module("vllm.x") style
        findings.extend(
            f"{path}:{getattr(node, 'lineno', 0)}: imports {name}"
            for name in names
            if name.split(".")[0] in DISALLOWED_MODULES
        )
    return findings


def scan(workspace: Path) -> list[str]:
    """Return human-readable findings; empty means no static evidence found."""
    findings: list[str] = []
    for path in _files(workspace):
        rel = path.relative_to(workspace)
        if any(part in DISALLOWED_MODULES for part in rel.parts[:-1]):
            findings.append(f"{rel}: vendored engine directory")
            continue
        if path.suffix == ".py":
            findings.extend(_module_findings(rel, path.read_text(errors="replace")))
        if path.suffix in _TEXT_SUFFIXES or path.suffix == ".py":
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if _INSTALL.search(line) and not line.lstrip().startswith(("#", "//")):
                    findings.append(f"{rel}:{number}: installs an engine: {line.strip()[:120]}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    findings = scan(args.workspace)
    for finding in findings:
        print(f"DISALLOWED ENGINE CODE: {finding}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
