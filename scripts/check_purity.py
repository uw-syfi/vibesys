"""D203 AST purity ratchet. Identities exclude lines and baseline may only shrink.

First introduction may recompute the legacy baseline when the explicit merge
base has no baseline. Once committed, additions and stale entries both fail.
No baseline entries are permitted for vs-core. This is a conservative syntax
check, complemented by tach boundaries and review of indirect dependencies.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from vs_project.api import run_git

BANNED_ROOTS = frozenset(
    {
        "aiohttp",
        "asyncio",
        "datetime",
        "ftplib",
        "http",
        "httpx",
        "multiprocessing",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "shutil",
        "socket",
        "ssl",
        "subprocess",
        "tempfile",
        "threading",
        "time",
        "urllib",
        "uuid",
    }
)
IO_ROOTS = frozenset(
    {
        "vs_agent",
        "vs_async_ops",
        "vs_evaluation",
        "vs_faults",
        "vs_github",
        "vs_issue_tracker",
        "vs_project",
        "vs_runtime",
        "vs_sandbox",
        "vs_slurm",
    }
)
IMPLEMENTATIONS = frozenset({"docker", "modal", "slurm", "claude-code", "omnigent"})
BUILTINS = frozenset({"open", "print", "input"})
LEGACY_SCOPE = (
    "src/vibesys/orchestration",
    "src/vibesys/domains",
    "src/vibesys/hypothesis",
    "src/vibesys/profile_focus",
    "src/vibesys/steering",
    "src/vibesys/prompts",
    "src/vibesys/metrics.py",
)
PURE_SCOPE = "libs/vs-core/src/vs_core"
BASELINE = "scripts/purity_violations.json"


@dataclass(frozen=True, order=True)
class Violation:
    """Stable violation identity; source line is deliberately not part of it."""

    path: str
    symbol: str
    rule: str
    subject: str
    occurrence: int


class PurityVisitor(ast.NodeVisitor):
    """Scan nested functions, class bodies and TYPE_CHECKING imports alike."""

    def __init__(self, path: str) -> None:
        """Start a file-local scanner with stable lexical identities."""
        self.path = path
        self.symbols: list[str] = []
        self.counts: Counter[tuple[str, str, str]] = Counter()
        self.violations: list[Violation] = []
        self.builtin_aliases = set(BUILTINS)
        self.builtin_modules = {"builtins"}

    def record(self, rule: str, subject: str) -> None:
        """Normalize identity and distinguish repeated identical sites."""
        symbol = ".".join(self.symbols) or "<module>"
        key = (symbol, rule, subject)
        self.counts[key] += 1
        self.violations.append(Violation(self.path, symbol, rule, subject, self.counts[key]))

    def visit_Import(self, node: ast.Import) -> None:
        """Aliasing does not exempt import roots."""
        for alias in node.names:
            self.check_import(alias.name)
            if alias.name == "builtins":
                self.builtin_modules.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Inspect imports regardless of execution guard."""
        module = node.module or ""
        self.check_import(module)
        if module == "builtins":
            for alias in node.names:
                if alias.name in BUILTINS:
                    self.builtin_aliases.add(alias.asname or alias.name)
                    self.record("builtin", alias.name)
        for alias in node.names:
            if alias.name == "wiring":
                self.record("implementation-import", f"{module}.wiring")

    def check_import(self, module: str) -> None:
        """Reject I/O modules and implementation exports using exact names."""
        root = module.split(".", maxsplit=1)[0]
        if root in BANNED_ROOTS:
            self.record("banned-import", module)
        if root in IO_ROOTS and not (
            module.endswith(".api.requests") and not self.path.startswith(PURE_SCOPE)
        ):
            self.record("io-import", module)
        if "wiring" in module.split("."):
            self.record("implementation-import", module)

    def visit_Name(self, node: ast.Name) -> None:
        """Taking a builtin reference is forbidden, including alias assignment."""
        if isinstance(node.ctx, ast.Load) and node.id in self.builtin_aliases:
            self.record("builtin", node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Recognize builtins.open through an aliased builtins module."""
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in self.builtin_modules
            and node.attr in BUILTINS
        ):
            self.record("builtin", node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """Only registered exact implementation names are forbidden literals."""
        if isinstance(node.value, str) and node.value in IMPLEMENTATIONS:
            self.record("implementation-literal", node.value)

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.symbols.append(node.name)
        self.generic_visit(node)
        self.symbols.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Keep lexical function identity stable across line changes."""
        self._scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Qualify methods with their owning class."""
        self._scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Async definitions cannot enter the pure core."""
        self.symbols.append(node.name)
        self.record("async-def", node.name)
        self.generic_visit(node)
        self.symbols.pop()

    def visit_Await(self, node: ast.Await) -> None:
        """Await syntax is forbidden even if the awaited name is pure."""
        self.record("await", ast.dump(node.value, include_attributes=False))
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """Async iteration is shell behavior."""
        self.record("async-for", ast.dump(node.iter, include_attributes=False))
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """Async resource scope belongs in the shell."""
        self.record("async-with", ast.dump(node.items[0].context_expr, include_attributes=False))
        self.generic_visit(node)


def scan_source(path: str, source: str) -> tuple[Violation, ...]:
    """Return stable violations; syntax errors fail closed."""
    visitor = PurityVisitor(path)
    visitor.visit(ast.parse(source, filename=path))
    return tuple(visitor.violations)


def scan_value_exports(root: Path) -> tuple[Violation, ...]:
    """Check the repository-local transitive closure of pure request exports."""
    modules: dict[str, Path] = {}
    for source in sorted((root / "libs").glob("*/src")):
        for path in sorted(source.rglob("*.py")):
            parts = path.relative_to(source).with_suffix("").parts
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            modules[name] = path
    pending = [name for name in modules if name.endswith(".api.requests")]
    visited: set[str] = set()
    violations: list[Violation] = []
    while pending:
        name = pending.pop()
        if name in visited or name not in modules:
            continue
        visited.add(name)
        path = modules[name]
        # Python executes parent packages before loading the requested module.
        parents = name.split(".")[:-1]
        pending.extend(".".join(parents[:index]) for index in range(1, len(parents) + 1))
        source = path.read_text()
        violations.extend(scan_source(path.relative_to(root).as_posix(), source))
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = (
                    package.split(".")[: len(package.split(".")) - node.level + 1]
                    if node.level
                    else []
                )
                target = ".".join((*prefix, node.module)) if node.module else ".".join(prefix)
                pending.append(target)
                pending.extend(f"{target}.{alias.name}" for alias in node.names)
    return tuple(violations)


def scan_repository(root: Path) -> tuple[Violation, ...]:
    """Scan the full legacy ratchet scope and the zero-waiver lifecycle core."""
    violations: list[Violation] = []
    for scope in (*LEGACY_SCOPE, PURE_SCOPE):
        location = root / scope
        paths = [location] if location.is_file() else sorted(location.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            violations.extend(scan_source(path.relative_to(root).as_posix(), path.read_text()))
    violations.extend(scan_value_exports(root))
    return tuple(sorted(set(violations)))


class BaselineError(ValueError):
    """A malformed ratchet cannot grant a purity exemption."""

    def __init__(self, path: str, detail: str) -> None:
        """Name the malformed baseline condition."""
        super().__init__(f"{path}: {detail}")


def read_baseline(source: str) -> frozenset[Violation]:
    """Reject malformed baseline keys rather than silently exempting a file."""
    data = json.loads(source)
    if not isinstance(data, list):
        raise BaselineError(BASELINE, "baseline must be a list of violation identities")
    entries = [Violation(**entry) for entry in data]
    if len(entries) != len(set(entries)):
        raise BaselineError(BASELINE, "duplicate baseline identity")
    return frozenset(entries)


def compare_baseline(
    actual: frozenset[Violation],
    baseline: frozenset[Violation],
    previous: frozenset[Violation] | None,
) -> tuple[str, ...]:
    """Fail new, stale, pure-waiver and growth sites independently."""
    errors = [f"new: {entry}" for entry in sorted(actual - baseline)]
    errors.extend(f"stale: {entry}" for entry in sorted(baseline - actual))
    errors.extend(
        f"pure waiver: {entry}" for entry in sorted(baseline) if entry.path.startswith(PURE_SCOPE)
    )
    if previous is not None:
        errors.extend(f"baseline growth: {entry}" for entry in sorted(baseline - previous))
    return tuple(errors)


def previous_baseline(root: Path, base_ref: str) -> frozenset[Violation] | None:
    """Require explicit valid merge-base context; allow only first introduction."""
    result = run_git(["merge-base", "HEAD", base_ref], cwd=root, text=True)
    result.check_returncode()
    base = result.stdout.strip()
    listing = run_git(["ls-tree", "--name-only", base, BASELINE], cwd=root, text=True)
    listing.check_returncode()
    if not listing.stdout.strip():
        return None
    source = run_git(["show", f"{base}:{BASELINE}"], cwd=root, text=True)
    source.check_returncode()
    return read_baseline(source.stdout)


def main() -> int:
    """Check the ratchet, or explicitly generate the initial reviewed snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    previous = previous_baseline(root, args.base_ref)
    actual = frozenset(scan_repository(root))
    if args.write:
        legacy = frozenset(entry for entry in actual if not entry.path.startswith(PURE_SCOPE))
        if previous is not None and legacy - previous:
            parser.error("cannot add violations to an established baseline")
        (root / BASELINE).write_text(
            json.dumps([asdict(entry) for entry in sorted(legacy)], indent=2) + "\n"
        )
    baseline = read_baseline((root / BASELINE).read_text())
    errors = compare_baseline(actual, baseline, previous)
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
