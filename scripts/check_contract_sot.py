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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Scan", "main", "measure", "ratchet"]

MANIFEST = "scripts/contract_replacements.json"
BASELINE = "scripts/contract_sot_baseline.jsonl"
GETATTR_ARGUMENTS = 2
MAX_ALIAS_TARGETS = 64
MAX_ALIAS_PASSES = 32
MAX_ALIAS_LENGTH = 512
MAX_RESOLUTION_PATHS = 256
AMBIGUOUS_ALIAS = "<ambiguous-alias>"
IMPORT_FUNCTIONS = frozenset({"importlib.import_module", "__import__", "builtins.__import__"})
GETATTR_FUNCTIONS = frozenset({"getattr", "builtins.getattr"})
ROOTS = ("src", "libs", "tests", "scripts", "sdk", "examples", "resources")
SKIP = frozenset({".venv", "__pycache__", "node_modules", ".git", "fixtures"})
type Key = tuple[str, str, str]
type Names = dict[str, frozenset[str]]
type QualifiedName = Annotated[str, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")]
type SymbolName = Annotated[str, Field(pattern=r"^[A-Za-z_]\w*$")]


class Replacement(BaseModel):
    """One strict defining authority and its declared canonical replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    module: QualifiedName
    symbol: SymbolName
    canonical: QualifiedName
    relation: Literal["exact", "split", "gap"]


class Manifest(BaseModel):
    """Versioned authority inventory, never permissively coerced at ingress."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: int = Field(ge=1, le=1)
    replacements: tuple[Replacement, ...]


class BaselineRecord(BaseModel):
    """One nonempty consumer and positive occurrence allowance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str = Field(min_length=1)
    module: QualifiedName
    symbol: SymbolName
    count: int = Field(gt=0)


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


def bind(result: Names, local: str, targets: frozenset[str]) -> bool:
    """Union possible static bindings so a later shadow cannot erase a use."""
    if not targets:
        return False
    previous = result.get(local, frozenset())
    combined = previous | targets
    if len(combined) > MAX_ALIAS_TARGETS or any(
        len(target) > MAX_ALIAS_LENGTH for target in combined
    ):
        combined = frozenset(
            sorted(target for target in combined if len(target) <= MAX_ALIAS_LENGTH)[
                :MAX_ALIAS_TARGETS
            ]
        ) | {AMBIGUOUS_ALIAS}
    result[local] = combined
    return result[local] != previous


def bindings(source: Source) -> Names:
    """Conservatively collect aliases and re-exports across all lexical scopes."""
    result: Names = {}
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name if alias.asname else alias.name.split(".")[0]
                bind(result, alias.asname or alias.name.split(".")[0], frozenset({target}))
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, source)
            for alias in node.names:
                if alias.name != "*":
                    bind(result, alias.asname or alias.name, frozenset({f"{base}.{alias.name}"}))
    return assignment_bindings(source.tree, result)


def assignment_bindings(tree: ast.Module, result: Names) -> Names:
    """Resolve assigned modules/functions and chained re-exports to a fixed point."""
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(min(len(assignments) + 1, MAX_ALIAS_PASSES)):
        changed = False
        for node in assignments:
            targets = assignment_targets(node.value, result)
            for local in node.targets:
                if isinstance(local, ast.Name):
                    changed |= bind(result, local.id, targets)
        if not changed:
            return result
    if assignments:
        result[AMBIGUOUS_ALIAS] = frozenset({AMBIGUOUS_ALIAS})
    return result


def resolve(name: str, exports: dict[str, Names]) -> frozenset[str]:
    """Follow every exported prefix, retaining alternatives from alias shadowing."""
    pending = [name]
    seen: set[str] = set()
    resolved: set[str] = set()
    while pending:
        current = pending.pop()
        if len(seen) >= MAX_RESOLUTION_PATHS or len(current) > MAX_ALIAS_LENGTH:
            resolved.add(AMBIGUOUS_ALIAS)
            break
        if current in seen:
            continue
        seen.add(current)
        parts = current.split(".")
        for index in range(len(parts) - 1, 0, -1):
            targets = exports.get(".".join(parts[:index]), {}).get(parts[index])
            if targets:
                pending.extend(".".join([target, *parts[index + 1 :]]) for target in targets)
                break
        else:
            resolved.add(current)
    return frozenset(resolved)


def literal_names(node: ast.AST | None, aliases: Names) -> frozenset[str]:
    """Resolve literal module strings and bounded static string aliases."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        return aliases.get(node.id, frozenset())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return frozenset(
            first + second
            for first in literal_names(node.left, aliases)
            for second in literal_names(node.right, aliases)
        )
    return frozenset()


def assignment_targets(node: ast.AST, aliases: Names) -> frozenset[str]:
    """Track static modules and imported values without guessing unknown locals."""
    if isinstance(node, (ast.Constant, ast.BinOp)):
        return frozenset(
            name
            for name in literal_names(node, {})
            if "." in name and all(part.isidentifier() for part in name.split("."))
        )
    name = dotted(node)
    if name is not None and name.split(".")[0] not in aliases:
        return frozenset()
    return qualified(node, aliases)


def qualified(node: ast.AST, aliases: Names) -> frozenset[str]:
    """Resolve every static alias and literal import/getattr expression."""
    if name := dotted(node):
        first, _, rest = name.partition(".")
        return frozenset(
            ".".join(filter(None, (target, rest)))
            for target in aliases.get(first, frozenset({first}))
        )
    if isinstance(node, ast.Attribute):
        return frozenset(f"{parent}.{node.attr}" for parent in qualified(node.value, aliases))
    if isinstance(node, ast.Call):
        return qualified_call(node, aliases)
    return frozenset()


def qualified_call(node: ast.Call, aliases: Names) -> frozenset[str]:
    """Resolve recognized dynamic-import and getattr calls without executing them."""
    functions = qualified(node.func, aliases)
    if functions & IMPORT_FUNCTIONS:
        return dynamic_import_names(node, aliases, functions)
    if functions & GETATTR_FUNCTIONS and len(node.args) >= GETATTR_ARGUMENTS:
        return frozenset(
            f"{parent}.{name}"
            for parent in qualified(node.args[0], aliases)
            for name in literal_names(node.args[1], aliases)
        )
    return frozenset()


def dynamic_import_names(
    node: ast.Call, aliases: Names, functions: frozenset[str]
) -> frozenset[str]:
    """Include built-in top-level imports and explicit relative package names."""
    names = literal_names(import_argument(node), aliases)
    package = (
        node.args[1]
        if len(node.args) > 1
        else next((keyword.value for keyword in node.keywords if keyword.arg == "package"), None)
    )
    packages = literal_names(package, aliases)
    result = {
        importlib.util.resolve_name(name, parent)
        for name in names
        if name.startswith(".")
        for parent in packages
    } | {name for name in names if not name.startswith(".")}
    if functions & {"__import__", "builtins.__import__"}:
        result.update(name.split(".")[0] for name in names if not name.startswith("."))
    return frozenset(result)


def import_argument(node: ast.Call) -> ast.AST | None:
    """One authoritative extraction for positional and keyword import names."""
    return (
        node.args[0]
        if node.args
        else next((keyword.value for keyword in node.keywords if keyword.arg == "name"), None)
    )


def fingerprint(node: ast.ClassDef) -> tuple[str, ...]:
    """Fingerprint declared fields and enum values, excluding names/docstrings."""
    fields = (child for child in node.body if isinstance(child, (ast.AnnAssign, ast.Assign)))
    return tuple(ast.dump(child, include_attributes=False) for child in fields)


def read_manifest(root: Path) -> dict[str, str]:
    """Read qualified frozen authorities through strict versioned metadata."""
    return manifest_authorities((root / MANIFEST).read_text())


def manifest_authorities(text: str) -> dict[str, str]:
    """Reject duplicate authority records after strict JSON schema validation."""
    data = Manifest.model_validate_json(text)
    result = {}
    for entry in data.replacements:
        name = f"{entry.module}.{entry.symbol}"
        if name in result:
            raise ContractGateError.invalid(name)
        result[name] = entry.canonical
    return result


def factory_bases(node: ast.Call, aliases: Names) -> tuple[ast.AST, ...]:
    """Recognize statically declared inheritance in standard model factories."""
    functions = qualified(node.func, aliases)
    if "pydantic.create_model" in functions:
        return tuple(keyword.value for keyword in node.keywords if keyword.arg == "__base__")
    if functions & {"type", "builtins.type"} and len(node.args) > 1:
        bases = node.args[1]
        if isinstance(bases, (ast.Tuple, ast.List)):
            return tuple(bases.elts)
    return ()


def copied_definitions(
    source: Source,
    authorities: dict[str, frozenset[str]],
    fingerprints: set[tuple[str, ...]],
    exports: dict[str, Names],
) -> list[str]:
    """Reject alternate named or structurally identical model definitions."""
    names = {name.rpartition(".")[2] for name in authorities}
    canonical = set().union(*authorities.values())
    errors = []
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Call):
            bases = {
                owner
                for base in factory_bases(node, exports[source.module])
                for target in qualified(base, exports[source.module])
                for owner in resolve(target, exports)
            }
            if bases.intersection(authorities):
                errors.append(
                    f"{source.path}:{node.lineno}: copied frozen contract by model factory"
                )
        if not isinstance(node, ast.ClassDef):
            continue
        name = f"{source.module}.{node.name}"
        # Provider process state is a different domain from durable orchestration.
        if name == "vs_agent.drivers._omnigent_lifecycle.LifecycleState":
            continue
        if name in authorities or name in canonical:
            continue
        signature = fingerprint(node)
        bases = {
            owner
            for base in node.bases
            for target in qualified(base, exports[source.module])
            for owner in resolve(target, exports)
        }
        if (
            bases.intersection(authorities)
            or node.name in names
            or (len(signature) >= GETATTR_ARGUMENTS and signature in fingerprints)
        ):
            errors.append(f"{source.path}:{node.lineno}: copied frozen contract {node.name}")
    return errors


def uncertain_bindings(tree: ast.Module, aliases: Names) -> frozenset[str]:
    """Arguments and computed writes cannot prove a dynamic import string."""
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            result.add(node.arg)
        elif isinstance(node, ast.Assign) and not assignment_targets(node.value, aliases):
            result.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            result.add(node.target.id)
    return frozenset(result)


def source_counts(
    source: Source, aliases: Names, exports: dict[str, Names], frozen: set[str]
) -> Scan:
    """Measure each syntactic import/access once per possible defining authority."""
    counts: Counter[Key] = Counter()
    errors = [
        f"{source.path}: static aliases exceed the bounded resolver"
        for targets in aliases.values()
        if AMBIGUOUS_ALIAS in targets
    ]
    legacy_aliases = any(
        any(
            target.startswith(module + ".") or target == module for module in frozen_modules(frozen)
        )
        for targets in aliases.values()
        for target in targets
    )
    uncertain = uncertain_bindings(source.tree, aliases)
    for node in ast.walk(source.tree):
        names: set[str] = set()
        if isinstance(node, ast.ImportFrom):
            base = import_base(node, source)
            for alias in node.names:
                if alias.name == "*":
                    errors.append(f"{source.path}:{node.lineno}: star import from {base}")
                else:
                    names.add(f"{base}.{alias.name}")
        elif isinstance(node, (ast.Attribute, ast.Call)):
            names.update(qualified(node, aliases))
            if isinstance(node, ast.Call) and unsafe_dynamic_import(
                node, aliases, frozen, legacy_aliases=legacy_aliases, uncertain=uncertain
            ):
                errors.append(
                    f"{source.path}:{node.lineno}: computed legacy import cannot be resolved"
                )
        owners = {owner for name in names for owner in resolve(name, exports)}
        if AMBIGUOUS_ALIAS in owners:
            errors.append(f"{source.path}:{node.lineno}: re-exports exceed the bounded resolver")
        for owner in owners & frozen:
            module, _, symbol = owner.rpartition(".")
            counts[(source.path, module, symbol)] += 1
    return Scan(counts, tuple(errors))


def frozen_modules(frozen: set[str]) -> frozenset[str]:
    """Qualified modules containing frozen definitions."""
    return frozenset(name.rpartition(".")[0] for name in frozen)


def static_import_argument(node: ast.AST | None, aliases: Names, uncertain: frozenset[str]) -> bool:
    """Recognize bounded static string arguments rather than evaluating code."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return bool(aliases.get(node.id)) and node.id not in uncertain
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and static_import_argument(node.left, aliases, uncertain)
        and static_import_argument(node.right, aliases, uncertain)
    )


def unsafe_dynamic_import(
    node: ast.Call,
    aliases: Names,
    frozen: set[str],
    *,
    legacy_aliases: bool,
    uncertain: frozenset[str],
) -> bool:
    """Reject unresolved dynamic imports with explicit legacy scope evidence."""
    if not qualified(node.func, aliases) & IMPORT_FUNCTIONS:
        return False
    arg = import_argument(node)
    if static_import_argument(arg, aliases, uncertain):
        return False
    strings = (
        {
            child.value
            for child in ast.walk(arg)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
        if arg is not None
        else set()
    )
    explicit = any(
        text and (text.startswith(module) or text.rstrip(".") == module.rpartition(".")[0])
        for text in strings
        for module in frozen_modules(frozen)
    )
    return legacy_aliases or explicit


def canonical_errors(
    authorities: dict[str, str], parsed: tuple[Source, ...], exports: dict[str, Names]
) -> list[str]:
    """Require every replacement name to be an actual published static export."""
    api = next((source for source in parsed if source.module == "vs_core.api"), None)
    declared: set[str] = set()
    if api is not None:
        for node in api.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            ):
                declared.update(
                    child.value
                    for child in ast.walk(node.value)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                )
    definitions = {
        f"{source.module}.{node.name.id if isinstance(node, ast.TypeAlias) else node.name}"
        for source in parsed
        for node in source.tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.TypeAlias))
    }
    errors = []
    for owner, canonical in authorities.items():
        if canonical == "vs_core.api" and api is not None:
            continue
        symbol = canonical.removeprefix("vs_core.api.")
        if (
            canonical.startswith("vs_core.api.")
            and symbol in declared
            and resolve(canonical, exports) & definitions
        ):
            continue
        errors.append(
            f"manifest {owner}: canonical {canonical} is not a published vs_core.api definition"
        )
    return errors


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
    errors = canonical_errors(authorities, parsed, exports)
    for source in parsed:
        scan = source_counts(source, exports[source.module], exports, set(authorities))
        counts.update(scan.counts)
        errors.extend(scan.errors)
        owners = {name: resolve(canonical, exports) for name, canonical in authorities.items()}
        errors.extend(copied_definitions(source, owners, fingerprints, exports))
    return Scan(counts, tuple(errors))


def decode_baseline(text: str) -> Counter[Key]:
    """Decode strict occurrence counts, rejecting duplicates and invalid counts."""
    counts: Counter[Key] = Counter()
    for line in text.splitlines():
        entry = BaselineRecord.model_validate_json(line)
        key = (entry.path, entry.module, entry.symbol)
        if key in counts:
            raise ContractGateError.invalid(key)
        counts[key] = entry.count
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


def base_file(root: Path, ref: str, filename: str) -> str | None:
    """Read fixed gate metadata at the merge base using Git's own parser."""
    git = shutil.which("git")
    if git is None or ref.startswith("-"):
        raise ContractGateError.invalid(ref)
    # lint-waiver: LW-090001 [S603]; Git reads the local baseline with argv and no shell. Reimplementing Git object/revision parsing or hard-coding a host executable would couple this gate to repository storage or host layout.
    base = subprocess.run(  # noqa: S603
        [git, "merge-base", ref, "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    # lint-waiver: LW-090002 [S603]; The resolved Git executable receives a validated merge-base object and a fixed baseline path. A Python Git-storage parser would duplicate Git's revision semantics.
    result = subprocess.run(  # noqa: S603
        [git, "show", f"{base}:{filename}"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def base_baseline(root: Path, ref: str) -> Counter[Key] | None:
    """Read the merge-base allowance; gate introduction has no previous file."""
    source = base_file(root, ref, BASELINE)
    return decode_baseline(source) if source is not None else None


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
        if (
            args.base_ref
            and (previous_manifest := base_file(args.root, args.base_ref, MANIFEST)) is not None
        ):
            removed = (
                manifest_authorities(previous_manifest).keys() - read_manifest(args.root).keys()
            )
            errors.extend(
                f"replacement manifest removed frozen authority {name}" for name in sorted(removed)
            )
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
