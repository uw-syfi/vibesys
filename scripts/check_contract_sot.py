#!/usr/bin/env python3
"""Freeze legacy contract consumers while migrations move authority to vs_core.api.

The manifest identifies frozen authorities. The JSONL baseline counts import
occurrences by consumer, defining module and symbol, without line numbers.
``--write`` only shrinks an existing baseline. CI also compares that baseline
with the merge base, so editing the file cannot increase its allowance.
Relative imports, re-exports, module aliases, literal dynamic imports and
qualified accesses resolve to the same authority. Copied named definitions and
renamed classes with an identical field fingerprint are rejected.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Scan", "main", "measure", "ratchet"]

MANIFEST = "scripts/contract_replacements.json"
BASELINE = "scripts/contract_sot_baseline.jsonl"
GETATTR_ARGUMENTS = 2
ROOTS = ("src", "libs", "tests", "scripts", "sdk", "examples", "resources")
SKIP = frozenset({".venv", "__pycache__", "node_modules", ".git", "fixtures"})
type Key = tuple[str, str, str]


@dataclass(frozen=True)
class Scan:
    """Measured legacy import occurrences and unbaselinable violations."""

    counts: Counter[Key]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    """Parsed module with its repository-relative consumer path."""

    path: str
    module: str
    package: str
    tree: ast.Module


class ContractGateError(ValueError):
    """Invalid manifest/baseline input, with the offending key or record."""

    @classmethod
    def invalid(cls, detail: object) -> ContractGateError:
        """Describe malformed gate configuration without hiding its location."""
        return cls(f"invalid contract gate input: {detail}")


def sources(root: Path) -> tuple[Source, ...]:
    """Discover source and consumer modules, including tests and TYPE_CHECKING."""
    result = []
    for directory in ROOTS:
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root)
            if SKIP.intersection(relative.parts):
                continue
            parts = list(relative.with_suffix("").parts)
            if "src" in parts:
                parts = parts[parts.index("src") + 1 :]
            package_file = parts[-1] == "__init__"
            if package_file:
                parts.pop()
            module = ".".join(parts)
            package = module if package_file else module.rpartition(".")[0]
            result.append(
                Source(str(relative), module, package, ast.parse(path.read_text(), str(relative)))
            )
    return tuple(result)


def dotted(node: ast.AST) -> str | None:
    """Extract a statically named attribute chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (parent := dotted(node.value)):
        return f"{parent}.{node.attr}"
    return None


def import_base(node: ast.ImportFrom, source: Source) -> str:
    """Resolve Python relative import levels against the consumer package."""
    name = "." * node.level + (node.module or "")
    return importlib.util.resolve_name(name, source.package) if node.level else name


def bindings(source: Source) -> dict[str, str]:
    """Collect aliases and re-exports; preserve duplicate sites for measurement."""
    result = {}
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, source)
            for alias in node.names:
                if alias.name != "*":
                    result[alias.asname or alias.name] = f"{base}.{alias.name}"
    return assignment_bindings(source.tree, result)


def assignment_bindings(tree: ast.Module, result: dict[str, str]) -> dict[str, str]:
    """Extend a namespace with static module aliases and assigned re-exports."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and (target := qualified(node.value, result)):
            for local in node.targets:
                if isinstance(local, ast.Name):
                    result[local.id] = target
    return result


def resolve(name: str, exports: dict[str, dict[str, str]]) -> str:
    """Follow the longest exported prefix, including chained package exports."""
    seen = set()
    while name not in seen:
        seen.add(name)
        parts = name.split(".")
        replacement = None
        for index in range(len(parts) - 1, 0, -1):
            target = exports.get(".".join(parts[:index]), {}).get(parts[index])
            if target:
                replacement = ".".join([target, *parts[index + 1 :]])
                break
        if replacement is None:
            return name
        name = replacement
    return name


def qualified(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Resolve a local alias or a literal importlib expression to a dotted name."""
    if name := dotted(node):
        first, _, rest = name.partition(".")
        return ".".join(filter(None, (aliases.get(first, first), rest)))
    if isinstance(node, ast.Attribute) and (parent := qualified(node.value, aliases)):
        return f"{parent}.{node.attr}"
    if not isinstance(node, ast.Call):
        return None
    function = qualified(node.func, aliases)
    if function in {"importlib.import_module", "__import__"}:
        arg = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        arg = node.args[0] if node.args else arg
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    if function == "getattr" and len(node.args) >= GETATTR_ARGUMENTS:
        parent = qualified(node.args[0], aliases)
        name = node.args[1]
        if parent and isinstance(name, ast.Constant) and isinstance(name.value, str):
            return f"{parent}.{name.value}"
    return None


def fingerprint(node: ast.ClassDef) -> tuple[str, ...]:
    """Fingerprint declared fields and enum values, excluding names/docstrings."""
    fields = (child for child in node.body if isinstance(child, (ast.AnnAssign, ast.Assign)))
    return tuple(ast.dump(child, include_attributes=False) for child in fields)


def read_manifest(root: Path) -> dict[str, str]:
    """Read qualified frozen authorities and their canonical owner names."""
    data = json.loads((root / MANIFEST).read_text())
    if set(data) != {"schema_version", "replacements"} or data["schema_version"] != 1:
        raise ContractGateError.invalid(data)
    result = {}
    for entry in data["replacements"]:
        if set(entry) != {"module", "symbol", "canonical", "relation"}:
            raise ContractGateError.invalid(entry)
        if entry["relation"] not in {"exact", "split", "gap"}:
            raise ContractGateError.invalid(entry["relation"])
        name = f"{entry['module']}.{entry['symbol']}"
        if name in result:
            raise ContractGateError.invalid(name)
        result[name] = entry["canonical"]
    return result


def copied_definitions(
    source: Source, authorities: dict[str, str], fingerprints: set[tuple[str, ...]]
) -> list[str]:
    """Reject alternate named or structurally identical model definitions."""
    names = {name.rpartition(".")[2] for name in authorities}
    canonical = set(authorities.values())
    errors = []
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.ClassDef):
            continue
        name = f"{source.module}.{node.name}"
        # Provider process state is a different domain from durable orchestration.
        if name == "vs_agent.drivers._omnigent_lifecycle.LifecycleState":
            continue
        if name in authorities or name in canonical:
            continue
        signature = fingerprint(node)
        if node.name in names or (
            len(signature) >= GETATTR_ARGUMENTS and signature in fingerprints
        ):
            errors.append(f"{source.path}:{node.lineno}: copied frozen contract {node.name}")
    return errors


def source_counts(
    source: Source, aliases: dict[str, str], exports: dict[str, dict[str, str]], frozen: set[str]
) -> Scan:
    """Measure import sites plus module-qualified accesses, once per occurrence."""
    counts: Counter[Key] = Counter()
    errors = []
    for node in ast.walk(source.tree):
        names = []
        if isinstance(node, ast.ImportFrom):
            base = import_base(node, source)
            for alias in node.names:
                if alias.name == "*":
                    errors.append(f"{source.path}:{node.lineno}: star import from {base}")
                else:
                    names.append(f"{base}.{alias.name}")
        elif isinstance(node, (ast.Attribute, ast.Call)):
            target = qualified(node, aliases)
            # Directly imported symbols are counted at the import, not on every use.
            if target and (
                not dotted(node)
                or dotted(node).split(".")[0] not in aliases
                or aliases[dotted(node).split(".")[0]] not in frozen
            ):
                names.append(target)
        for name in names:
            owner = resolve(name, exports)
            if owner in frozen:
                module, _, symbol = owner.rpartition(".")
                counts[(source.path, module, symbol)] += 1
    return Scan(counts, tuple(errors))


def measure(root: Path) -> Scan:
    """Scan all consumers using the manifest, without importing application code."""
    authorities = read_manifest(root)
    parsed = sources(root)
    exports = {source.module: bindings(source) for source in parsed}
    fingerprints = {
        fingerprint(node)
        for source in parsed
        for node in source.tree.body
        if isinstance(node, ast.ClassDef) and f"{source.module}.{node.name}" in authorities
    } - {()}
    counts: Counter[Key] = Counter()
    errors = []
    for source in parsed:
        scan = source_counts(source, exports[source.module], exports, set(authorities))
        counts.update(scan.counts)
        errors.extend(scan.errors)
        owners = {name: resolve(canonical, exports) for name, canonical in authorities.items()}
        errors.extend(copied_definitions(source, owners, fingerprints))
    return Scan(counts, tuple(errors))


def decode_baseline(text: str) -> Counter[Key]:
    """Decode strict occurrence counts, rejecting duplicates and invalid counts."""
    counts: Counter[Key] = Counter()
    for line in text.splitlines():
        entry = json.loads(line)
        if set(entry) != {"path", "module", "symbol", "count"}:
            raise ContractGateError.invalid(entry)
        key = (entry["path"], entry["module"], entry["symbol"])
        if key in counts or type(entry["count"]) is not int or entry["count"] <= 0:
            raise ContractGateError.invalid(key)
        counts[key] = entry["count"]
    return counts


def encode_baseline(counts: Counter[Key]) -> str:
    """Encode deterministic JSONL suitable for review and merge-base comparison."""
    return "".join(
        json.dumps({"path": p, "module": m, "symbol": s, "count": n}) + "\n"
        for (p, m, s), n in sorted(counts.items())
    )


def ratchet(current: Counter[Key], baseline: Counter[Key]) -> tuple[str, ...]:
    """Return every new or grown occurrence; removed entries always shrink."""
    return tuple(
        f"{key}: {count} occurrences exceed baseline {baseline[key]}"
        for key, count in sorted(current.items())
        if count > baseline[key]
    )


def base_baseline(root: Path, ref: str) -> Counter[Key] | None:
    """Read the merge-base baseline; before gate introduction no file exists."""
    git = shutil.which("git")
    if git is None or ref.startswith("-"):
        raise ContractGateError.invalid(ref)
    # lint-waiver: LW-090001 [S603]; Git reads the local baseline with argv and no shell. Reimplementing Git object/revision parsing or hard-coding a host executable would couple this gate to repository storage or host layout.
    base = subprocess.run(  # noqa: S603
        [git, "merge-base", ref, "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    # lint-waiver: LW-090002 [S603]; The resolved Git executable receives a validated merge-base object and a fixed baseline path. A Python Git-storage parser would duplicate Git's revision semantics.
    result = subprocess.run(  # noqa: S603
        [git, "show", f"{base}:{BASELINE}"], cwd=root, capture_output=True, text=True, check=False
    )
    return decode_baseline(result.stdout) if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    """Check exact remaining uses or shrink the baseline; return a CLI exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--base-ref")
    args = parser.parse_args(argv)
    try:
        scan = measure(args.root)
        path = args.root / BASELINE
        baseline = decode_baseline(path.read_text())
        errors = [*scan.errors, *ratchet(scan.counts, baseline)]
        if args.base_ref and (original := base_baseline(args.root, args.base_ref)) is not None:
            errors.extend(ratchet(baseline, original))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        if args.write:
            path.write_text(encode_baseline(scan.counts))
        elif scan.counts != baseline:
            print("contract baseline is stale; run check_contract_sot.py --write", file=sys.stderr)
            return 1
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"check_contract_sot: {exc}", file=sys.stderr)
        return 2
    print(f"contract SOT: {len(scan.counts)} tuples, {sum(scan.counts.values())} occurrences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
