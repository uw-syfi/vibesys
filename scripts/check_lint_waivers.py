#!/usr/bin/env python3
"""Check that every Ruff suppression has a reason and a manifest entry.

The manifest uses stable waiver IDs rather than source line numbers. Each ID
appears in a comment immediately before its ``# noqa`` directive, with a short
explanation of why the rule is suppressed. The check parses Ruff's configured Python
file set, tokenizes comments so strings and docstrings do not count, and uses
the Python AST to ensure each suppression belongs to a source node.

Usage:
    uv run python scripts/check_lint_waivers.py.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
import tokenize
import tomllib
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Iterable

DEFAULT_MANIFEST = Path("lint_waivers.jsonl")
DEFAULT_PYPROJECT = Path("pyproject.toml")
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
MIN_REASON_LENGTH = 12
NOQA_RE = re.compile(
    r"#\s*(?:ruff:\s*)?noqa\b(?::\s*([A-Z][A-Z0-9]*(?:\s*,\s*[A-Z][A-Z0-9]*)*))?",
    re.IGNORECASE,
)
WAIVER_RE = re.compile(r"#\s*(?:lint-waiver:\s*)?(LW-\d{6})(?:\s+\[([A-Z0-9, ]+)\])?\s*;\s*(.*)$")
WAIVER_CONTINUATION_RE = re.compile(r"#\s*(?:lint-waiver\+:|>\s*)(.*)$")
WAIVER_ID_RE = re.compile(r"LW-\d{6}\Z")

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2


@dataclass(frozen=True)
class ManifestWaiver:
    """One tracked suppression site."""

    waiver_id: str
    path: str
    rules: tuple[str, ...]


@dataclass(frozen=True)
class SourceWaiver:
    """A suppression discovered in a Python comment token."""

    waiver_id: str
    path: str
    rules: tuple[str, ...]
    line: int
    reason: str


def parse_manifest(path: Path, repo_root: Path) -> tuple[list[ManifestWaiver], list[str]]:
    """Load JSONL entries and report malformed or duplicate IDs."""
    failures: list[str] = []
    waivers: list[ManifestWaiver] = []
    ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"{path}: cannot be read ({exc})"]

    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError as exc:
            failures.append(f"{path}:{line_number}: invalid JSON ({exc.msg})")
            continue
        waiver, error = parse_manifest_entry(value, repo_root, path, line_number)
        if error is not None:
            failures.append(error)
        if waiver is None:
            continue
        waiver_id = waiver.waiver_id
        if waiver_id in ids:
            failures.append(f"{path}:{line_number}: duplicate id {waiver_id}")
            continue
        ids.add(waiver_id)
        waivers.append(waiver)
    return waivers, failures


def parse_manifest_entry(
    value: object, repo_root: Path, manifest_path: Path, line_number: int
) -> tuple[ManifestWaiver | None, str | None]:
    """Validate one manifest object and convert it to a typed waiver."""
    if not isinstance(value, dict):
        return None, f"{manifest_path}:{line_number}: entry must be a JSON object"
    waiver_id = value.get("id")
    relative_path = value.get("path")
    rules = value.get("rules")
    if not isinstance(waiver_id, str) or not WAIVER_ID_RE.fullmatch(waiver_id):
        return None, f"{manifest_path}:{line_number}: id must match LW-NNNNNN"
    if not isinstance(relative_path, str) or not relative_path:
        return None, f"{manifest_path}:{line_number}: path must be a non-empty string"
    if not _valid_manifest_path(repo_root, relative_path):
        return None, f"{manifest_path}:{line_number}: path must name a repository Python file"
    if not _valid_rules(rules):
        return None, f"{manifest_path}:{line_number}: rules must be a non-empty unique list"
    return ManifestWaiver(waiver_id, Path(relative_path).as_posix(), tuple(sorted(rules))), None


def _valid_manifest_path(repo_root: Path, relative_path: str) -> bool:
    candidate = (repo_root / relative_path).resolve()
    return candidate.is_relative_to(repo_root.resolve()) and candidate.suffix == ".py"


def _valid_rules(rules: object) -> TypeGuard[list[str]]:
    return (
        isinstance(rules, list)
        and bool(rules)
        and all(isinstance(rule, str) and rule for rule in rules)
        and len(set(rules)) == len(rules)
    )


def discover_ruff_files(repo_root: Path) -> tuple[list[Path], str | None]:
    """Discover Python files using Ruff's configured exclusion patterns."""
    try:
        pyproject = tomllib.loads((repo_root / DEFAULT_PYPROJECT).read_text(encoding="utf-8"))
        excluded = pyproject.get("tool", {}).get("ruff", {}).get("extend-exclude", [])
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], f"cannot read Ruff exclusions from pyproject.toml ({exc})"
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded):
        return [], "[tool.ruff].extend-exclude must be a list of path patterns"
    files = []
    for path in repo_root.rglob("*.py"):
        relative = path.relative_to(repo_root).as_posix()
        if DEFAULT_EXCLUDED_DIRS.intersection(path.relative_to(repo_root).parts):
            continue
        if any(_matches_exclusion(relative, pattern) for pattern in excluded):
            continue
        files.append(path.resolve())
    return sorted(files), None


def _matches_exclusion(relative: str, pattern: str) -> bool:
    """Match Ruff-style exact/prefix exclusions and glob patterns."""
    if not any(character in pattern for character in "*?["):
        return relative == pattern or relative.startswith(f"{pattern.rstrip('/')}/")
    return fnmatch.fnmatchcase(relative, pattern)


def _smallest_ast_node_at_line(tree: ast.Module, line: int) -> ast.AST | None:
    """Return the narrowest syntax node that contains ``line``."""
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(getattr(node, "lineno", None), int)
        and isinstance(getattr(node, "end_lineno", None), int)
        and node.lineno <= line <= node.end_lineno
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda node: (node.end_lineno - node.lineno, -getattr(node, "col_offset", 0)),
    )


def _comment_is_attached(tree: ast.Module, line: int) -> bool:
    """Allow an adjacent waiver comment before a node or its decorators."""
    return any(_smallest_ast_node_at_line(tree, line + offset) is not None for offset in range(32))


def _waiver_reason(marker: re.Match[str], line: int, comments_by_line: dict[int, str]) -> str:
    """Read a waiver reason and its optional continuation comments."""
    parts = [marker.group(3).strip()]
    continuation_line = line + 1
    while continuation_line in comments_by_line:
        continuation = WAIVER_CONTINUATION_RE.fullmatch(comments_by_line[continuation_line].strip())
        if continuation is None:
            break
        parts.append(continuation.group(1).strip())
        continuation_line += 1
    return " ".join(part for part in parts if part)


def scan_source_file(path: Path, repo_root: Path) -> tuple[list[SourceWaiver], list[str]]:
    """Find Ruff ``noqa`` directives and validate their waiver records."""
    relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        tokens = list(tokenize.generate_tokens(StringIO(source).readline))
    except (OSError, SyntaxError, tokenize.TokenError, UnicodeDecodeError) as exc:
        return [], [f"{relative}: cannot tokenize Python source ({exc})"]
    directives, failures = _source_directives(tokens, relative)
    comments_by_line = {
        token.start[0]: token.string for token in tokens if token.type == tokenize.COMMENT
    }
    waivers, waiver_failures = _source_waivers(tokens, tree, relative, comments_by_line, directives)
    failures.extend(waiver_failures)
    if not failures and _rule_counts(directives.values()) != _rule_counts(
        waiver.rules for waiver in waivers
    ):
        failures.append(
            f"{relative}: source waiver comments do not match its noqa directive counts"
        )
    return waivers, failures


def _source_directives(
    tokens: list[tokenize.TokenInfo], relative: str
) -> tuple[dict[int, tuple[str, ...]], list[str]]:
    directives: dict[int, tuple[str, ...]] = {}
    failures: list[str] = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        directive = NOQA_RE.search(token.string)
        if directive is None:
            continue
        line = token.start[0]
        rules_text = directive.group(1)
        if not rules_text:
            failures.append(f"{relative}:{line}: noqa must name one or more Ruff rules")
            continue
        directives[line] = tuple(sorted({rule.strip().upper() for rule in rules_text.split(",")}))
    return directives, failures


def _source_waivers(
    tokens: list[tokenize.TokenInfo],
    tree: ast.Module,
    relative: str,
    comments_by_line: dict[int, str],
    directives: dict[int, tuple[str, ...]],
) -> tuple[list[SourceWaiver], list[str]]:
    waivers: list[SourceWaiver] = []
    failures: list[str] = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        markers = list(WAIVER_RE.finditer(token.string))
        if not markers:
            continue
        if len(markers) != 1:
            failures.append(f"{relative}:{line}: source comment needs exactly one waiver ID")
            continue
        marker = markers[0]
        marker_rules = marker.group(2)
        rules = directives.get(line)
        if marker_rules:
            rules = tuple(sorted({rule.strip().upper() for rule in marker_rules.split(",")}))
        if rules is None:
            failures.append(f"{relative}:{line}: detached waiver comment must name its Ruff rules")
            continue
        reason = _waiver_reason(marker, line, comments_by_line)
        if len(reason) < MIN_REASON_LENGTH:
            failures.append(f"{relative}:{line}: waiver reason must contain at least 12 characters")
            continue
        if (
            not _comment_is_attached(tree, line)
            and "ERA001" not in rules
            and not token.string.lower().startswith("# ruff: noqa")
        ):
            failures.append(f"{relative}:{line}: waiver comment is not attached to Python syntax")
            continue
        waiver_id = marker.group(1)
        waivers.append(SourceWaiver(waiver_id, relative, rules, line, reason))
    return waivers, failures


def _rule_counts(rulesets: Iterable[tuple[str, ...]]) -> dict[tuple[str, ...], int]:
    """Count suppressions by their exact sorted rule tuple."""
    counts: dict[tuple[str, ...], int] = {}
    for rules in rulesets:
        counts[rules] = counts.get(rules, 0) + 1
    return counts


def audit(
    repo_root: Path,
    files: list[Path],
    manifest_waivers: list[ManifestWaiver],
    manifest_failures: list[str] | None = None,
) -> list[str]:
    """Compare source comments to the central manifest."""
    failures = list(manifest_failures or [])
    found: dict[str, SourceWaiver] = {}
    for path in files:
        source_waivers, source_failures = scan_source_file(path, repo_root)
        failures.extend(source_failures)
        for waiver in source_waivers:
            if waiver.waiver_id in found:
                previous = found[waiver.waiver_id]
                failures.append(
                    f"{waiver.path}:{waiver.line}: {waiver.waiver_id} is also used at "
                    f"{previous.path}:{previous.line}"
                )
                continue
            found[waiver.waiver_id] = waiver

    recorded = {waiver.waiver_id: waiver for waiver in manifest_waivers}
    for waiver_id, waiver in sorted(found.items()):
        entry = recorded.get(waiver_id)
        if entry is None:
            failures.append(
                f"{waiver.path}:{waiver.line}: {waiver_id} is missing from the manifest"
            )
        elif entry.path != waiver.path or entry.rules != waiver.rules:
            failures.append(
                f"{waiver.path}:{waiver.line}: {waiver_id} does not match its manifest path/rules"
            )
    for waiver_id, entry in sorted(recorded.items()):
        if waiver_id not in found:
            failures.append(f"{entry.path}: manifest waiver {waiver_id} has no source comment")
    return failures


def main() -> int:
    """Run the manifest/source audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--manifest", type=Path, default=None, help="waiver JSONL path")
    args = parser.parse_args()
    repo_root = args.root.resolve()
    manifest_path = args.manifest or repo_root / DEFAULT_MANIFEST
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path

    manifest_waivers, manifest_failures = parse_manifest(manifest_path, repo_root)
    files, discovery_error = discover_ruff_files(repo_root)
    if discovery_error is not None:
        print(f"check_lint_waivers: {discovery_error}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    failures = audit(repo_root, files, manifest_waivers, manifest_failures)
    if failures:
        print("Lint waiver manifest violations:")
        for failure in failures:
            print(f"  {failure}")
        return EXIT_VIOLATIONS
    print(f"All {len(manifest_waivers)} Ruff waiver(s) have a unique manifest entry and reason.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
