#!/usr/bin/env python3
"""Freeze legacy contract consumers while migrations move authority to vs_core.api.

The manifest identifies frozen authorities. The JSONL baseline counts import
occurrences by consumer, defining module and symbol, without line numbers.
``--write`` only shrinks an existing baseline. CI also compares that baseline
with the merge base, so editing the file cannot increase its allowance.
Relative imports, re-exports, module aliases, literal dynamic imports and
qualified accesses resolve to the same authority. Copied named definitions and
renamed classes with an identical field fingerprint are rejected. Original
class shapes survive deletion; fields and enum values cannot grow, and retired
authorities cannot reappear.
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


class ClassShape(BaseModel):
    """Permanent seed declarations plus a monotonically retired authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    bases: tuple[str, ...]
    members: tuple[str, ...]
    retired: bool


class Replacement(BaseModel):
    """One strict defining authority and its declared canonical replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    module: QualifiedName
    symbol: SymbolName
    canonical: QualifiedName
    relation: Literal["exact", "split", "gap"]
    shape: ClassShape | None


class Manifest(BaseModel):
    """Versioned authority inventory, never permissively coerced at ingress."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: int = Field(ge=2, le=2)
    seed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
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
    assignments = [
        (
            local,
            value,
            frozenset(parameter.name for parameter in node.type_params)
            if isinstance(node, ast.TypeAlias)
            else frozenset(),
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias, ast.NamedExpr))
        and node.value is not None
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.name if isinstance(node, ast.TypeAlias) else node.target]
        )
        for expression in assignment_values(node)
        for local, value in assignment_pairs(target, expression)
    ]
    for _ in range(min(len(assignments) + 1, MAX_ALIAS_PASSES)):
        changed = False
        for local, value, shadowed in assignments:
            aliases = {name: targets for name, targets in result.items() if name not in shadowed}
            changed |= bind(result, local, assignment_targets(value, aliases))
        if not changed:
            return result
    if assignments:
        result[AMBIGUOUS_ALIAS] = frozenset({AMBIGUOUS_ALIAS})
    return result


def assignment_values(
    node: ast.Assign | ast.AnnAssign | ast.TypeAlias | ast.NamedExpr,
) -> tuple[ast.AST, ...]:
    """Include generic alias bounds and defaults as type dependencies."""
    parameters = node.type_params if isinstance(node, ast.TypeAlias) else []
    values = [
        getattr(parameter, field, None)
        for parameter in parameters
        for field in ("bound", "default_value")
    ]
    dependencies = tuple(
        child
        for value in values
        if value is not None
        for child in (value.elts if isinstance(value, (ast.Tuple, ast.List)) else [value])
    )
    return ((node.value,) if node.value is not None else ()) + dependencies


def assignment_pairs(target: ast.AST, value: ast.AST) -> tuple[tuple[str, ast.AST], ...]:
    """Resolve names and exact structural unpacking, without guessing computed values."""
    if isinstance(target, ast.Name):
        return ((target.id, value),)
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        return tuple(
            pair
            for local, child in zip(target.elts, value.elts, strict=True)
            for pair in assignment_pairs(local, child)
        )
    return ()


def resolve(
    name: str, exports: dict[str, Names], stop_at: set[str] | None = None
) -> frozenset[str]:
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
        if stop_at and current in stop_at:
            resolved.add(current)
            continue
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


def assignment_targets(node: ast.AST, aliases: Names, depth: int = 0) -> frozenset[str]:
    """Resolve bounded static bindings and type dependencies without executing code."""
    if depth >= MAX_ALIAS_PASSES:
        return frozenset({AMBIGUOUS_ALIAS})
    children = expression_children(node, aliases)
    if depth and isinstance(node, (ast.Tuple, ast.List)):
        children = tuple(node.elts)
    elif depth and isinstance(node, ast.Starred):
        children = (node.value,)
    if children is not None:
        return frozenset(
            target for child in children for target in assignment_targets(child, aliases, depth + 1)
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return literal_assignment_names(node.value, aliases, depth)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_names(node, {})
    name = dotted(node)
    if name is not None and name.split(".")[0] not in aliases:
        return frozenset()
    return qualified(node, aliases)


def expression_children(node: ast.AST, aliases: Names) -> tuple[ast.AST, ...] | None:
    """Project actual type positions, excluding literal values and metadata."""
    if isinstance(node, ast.Subscript):
        constructors = qualified(node.value, aliases)
        if constructors & {"typing.Literal", "typing_extensions.Literal"}:
            return (node.value,)
        arguments = node.slice
        if constructors & {"typing.Annotated", "typing_extensions.Annotated"} and isinstance(
            arguments, ast.Tuple
        ):
            arguments = arguments.elts[0]
        children = arguments.elts if isinstance(arguments, (ast.Tuple, ast.List)) else [arguments]
        return (node.value, *children)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return (node.left, node.right)
    if isinstance(node, (ast.IfExp, ast.NamedExpr)):
        return (node.value,) if isinstance(node, ast.NamedExpr) else (node.body, node.orelse)
    if isinstance(node, ast.Call) and qualified(node.func, aliases) & {
        "typing.TypeAliasType",
        "typing_extensions.TypeAliasType",
    }:
        return tuple(node.args[1:2]) or tuple(
            keyword.value for keyword in node.keywords if keyword.arg == "value"
        )
    return None


def literal_assignment_names(text: str, aliases: Names, depth: int) -> frozenset[str]:
    """Keep literal import paths and resolve quoted forward-reference types."""
    literal = (
        frozenset({text})
        if "." in text and all(part.isidentifier() for part in text.split("."))
        else frozenset()
    )
    prefix = text.partition(".")[0]
    if literal & aliases.get(prefix, frozenset()):
        aliases = {**aliases, prefix: aliases[prefix] - literal}
    try:
        expression = ast.parse(text, mode="eval").body
    except SyntaxError:
        return literal
    # Quoted literals are values, not another recursive forward-reference layer.
    if isinstance(expression, ast.Constant):
        return literal
    return literal | assignment_targets(expression, aliases, depth + 1)


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
    return tuple(sorted(ast.dump(child, include_attributes=False) for child in fields))


def read_manifest(root: Path) -> dict[str, str]:
    """Read qualified frozen authorities through strict versioned metadata."""
    return manifest_authorities((root / MANIFEST).read_text())


def manifest_authorities(text: str) -> dict[str, str]:
    """Reject duplicate authority records after strict JSON schema validation."""
    data = decode_manifest(text)
    result = {}
    for entry in data.replacements:
        name = f"{entry.module}.{entry.symbol}"
        if name in result:
            raise ContractGateError.invalid(name)
        result[name] = entry.canonical
    return result


def decode_manifest(text: str) -> Manifest:
    """Validate frozen shape metadata before any source comparison."""
    data = Manifest.model_validate_json(text)
    owners = [f"{entry.module}.{entry.symbol}" for entry in data.replacements]
    if len(set(owners)) != len(owners):
        raise ContractGateError.invalid(owners)
    return data


def class_definitions(parsed: tuple[Source, ...]) -> dict[str, ast.ClassDef]:
    """Locate top-level defining classes without following consumer re-exports."""
    return {
        f"{source.module}.{node.name}": node
        for source in parsed
        for node in source.tree.body
        if isinstance(node, ast.ClassDef)
    }


def class_bases(node: ast.ClassDef) -> tuple[str, ...]:
    """Fingerprint declared inheritance and class metaclass keyword arguments."""
    return tuple(ast.dump(base, include_attributes=False) for base in [*node.bases, *node.keywords])


def shape_errors(manifest: Manifest, definitions: dict[str, ast.ClassDef]) -> list[str]:
    """Reject growth, declaration changes, and reintroduction of seeded classes."""
    errors = []
    for entry in manifest.replacements:
        name = f"{entry.module}.{entry.symbol}"
        node = definitions.get(name)
        if entry.shape is None:
            if node is not None:
                errors.append(f"{name}: frozen non-class authority became a class")
            continue
        if node is None:
            continue
        if entry.shape.retired:
            errors.append(f"{name}: retired frozen authority was reintroduced")
        if class_bases(node) != entry.shape.bases:
            errors.append(f"{name}: frozen class inheritance changed")
        if not set(fingerprint(node)).issubset(entry.shape.members):
            errors.append(f"{name}: frozen fields or enum values grew or changed")
    return errors


def retirement_manifest(root: Path) -> Manifest:
    """Advance retirement only when a seeded defining class has disappeared."""
    manifest = decode_manifest((root / MANIFEST).read_text())
    definitions = class_definitions(sources(root))
    entries = tuple(
        entry.model_copy(update={"shape": entry.shape.model_copy(update={"retired": True})})
        if entry.shape is not None and f"{entry.module}.{entry.symbol}" not in definitions
        else entry
        for entry in manifest.replacements
    )
    return manifest.model_copy(update={"replacements": entries})


def manifest_ratchet(current: Manifest, previous: Manifest) -> tuple[str, ...]:
    """Keep original shape provenance immutable and retirement monotonic."""
    errors = []
    if current.seed_commit != previous.seed_commit:
        errors.append("replacement manifest changed frozen seed_commit")
    now = {f"{entry.module}.{entry.symbol}": entry for entry in current.replacements}
    for original in previous.replacements:
        name = f"{original.module}.{original.symbol}"
        entry = now.get(name)
        if entry is None:
            errors.append(f"replacement manifest removed frozen authority {name}")
            continue
        shape, old_shape = entry.shape, original.shape
        if old_shape is None and shape is None:
            continue
        if (
            shape is None
            or old_shape is None
            or shape.bases != old_shape.bases
            or shape.members != old_shape.members
        ):
            errors.append(f"{name}: replacement manifest changed frozen class shape")
        elif old_shape.retired and not shape.retired:
            errors.append(f"{name}: replacement manifest reversed retirement")
    return tuple(errors)


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
        owners = {owner for name in names for owner in resolve(name, exports, frozen)}
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
            and resolve(canonical, exports, definitions) & definitions
        ):
            continue
        errors.append(
            f"manifest {owner}: canonical {canonical} is not a published vs_core.api definition"
        )
    return errors


def measure(root: Path) -> Scan:
    """Scan all consumers using the manifest, without importing application code."""
    manifest = decode_manifest((root / MANIFEST).read_text())
    authorities = read_manifest(root)
    parsed = sources(root)
    exports = {source.module: bindings(source) for source in parsed}
    fingerprints = {entry.shape.members for entry in manifest.replacements if entry.shape} | {
        fingerprint(node)
        for source in parsed
        for node in source.tree.body
        if isinstance(node, ast.ClassDef) and f"{source.module}.{node.name}" in authorities
    } - {()}
    counts: Counter[Key] = Counter()
    errors = canonical_errors(authorities, parsed, exports)
    errors.extend(shape_errors(manifest, class_definitions(parsed)))
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
            errors.extend(
                manifest_ratchet(
                    decode_manifest((args.root / MANIFEST).read_text()),
                    decode_manifest(previous_manifest),
                )
            )
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        retired = retirement_manifest(args.root)
        manifest_path = args.root / MANIFEST
        if args.write:
            path.write_text(encode_baseline(scan.counts))
            manifest_path.write_text(retired.model_dump_json(indent=2) + "\n")
        elif retired != decode_manifest(manifest_path.read_text()):
            print(
                "contract retirement metadata is stale; run check_contract_sot.py --write",
                file=sys.stderr,
            )
            return 1
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
