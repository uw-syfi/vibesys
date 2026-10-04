"""D203 AST purity ratchet. Identities exclude lines and baseline may only shrink.

First introduction may recompute the legacy baseline when the explicit merge
base has no baseline. Once committed, additions and stale entries both fail.
No baseline entries are permitted for vs-core. This is a conservative syntax
check, complemented by tach boundaries and review of indirect dependencies.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from vs_project.api import run_git

# Each export is reviewed as deterministic value construction or transformation.
# Approving a module does not approve future exports or its imported dependencies.
PURE_EXPORTS = {
    "__future__": frozenset({"annotations"}),
    "abc": frozenset({"ABC", "ABCMeta", "abstractmethod"}),
    "collections": frozenset({"Counter", "OrderedDict", "defaultdict", "deque", "namedtuple"}),
    "collections.abc": frozenset(
        {
            "Callable",
            "Collection",
            "Container",
            "Generator",
            "Hashable",
            "Iterable",
            "Iterator",
            "Mapping",
            "MutableMapping",
            "MutableSequence",
            "MutableSet",
            "Reversible",
            "Sequence",
            "Set",
            "Sized",
        }
    ),
    "dataclasses": frozenset(
        {"asdict", "astuple", "dataclass", "field", "fields", "is_dataclass", "replace"}
    ),
    "enum": frozenset({"Enum", "IntEnum", "IntFlag", "Flag", "StrEnum", "auto", "unique"}),
    "functools": frozenset(
        {"cache", "cached_property", "cmp_to_key", "lru_cache", "partial", "reduce", "wraps"}
    ),
    "hashlib": frozenset(
        {
            "blake2b",
            "blake2s",
            "md5",
            "sha1",
            "sha224",
            "sha256",
            "sha384",
            "sha512",
            "sha3_256",
            "sha3_512",
        }
    ),
    "itertools": frozenset(
        {
            "accumulate",
            "batched",
            "chain",
            "combinations",
            "combinations_with_replacement",
            "compress",
            "count",
            "cycle",
            "dropwhile",
            "filterfalse",
            "groupby",
            "islice",
            "pairwise",
            "permutations",
            "product",
            "repeat",
            "starmap",
            "takewhile",
            "tee",
            "zip_longest",
        }
    ),
    "json": frozenset({"JSONDecodeError", "JSONDecoder", "JSONEncoder", "dumps", "loads"}),
    "math": frozenset(
        {
            "acos",
            "acosh",
            "asin",
            "asinh",
            "atan",
            "atan2",
            "atanh",
            "ceil",
            "comb",
            "copysign",
            "cos",
            "cosh",
            "degrees",
            "dist",
            "e",
            "erf",
            "erfc",
            "exp",
            "expm1",
            "fabs",
            "factorial",
            "floor",
            "fmod",
            "frexp",
            "fsum",
            "gamma",
            "gcd",
            "hypot",
            "inf",
            "isclose",
            "isfinite",
            "isinf",
            "isnan",
            "isqrt",
            "lcm",
            "ldexp",
            "lgamma",
            "log",
            "log10",
            "log1p",
            "log2",
            "modf",
            "nan",
            "nextafter",
            "perm",
            "pi",
            "pow",
            "prod",
            "radians",
            "remainder",
            "sin",
            "sinh",
            "sqrt",
            "tan",
            "tanh",
            "tau",
            "trunc",
            "ulp",
        }
    ),
    "operator": frozenset(
        {
            "add",
            "and_",
            "attrgetter",
            "concat",
            "contains",
            "countOf",
            "eq",
            "floordiv",
            "ge",
            "gt",
            "index",
            "indexOf",
            "inv",
            "invert",
            "is_",
            "is_not",
            "itemgetter",
            "le",
            "length_hint",
            "lshift",
            "lt",
            "matmul",
            "mod",
            "mul",
            "ne",
            "neg",
            "not_",
            "or_",
            "pos",
            "pow",
            "rshift",
            "sub",
            "truediv",
            "truth",
            "xor",
        }
    ),
    "pydantic": frozenset(
        {
            "AfterValidator",
            "BaseModel",
            "BeforeValidator",
            "ConfigDict",
            "Field",
            "FiniteFloat",
            "InstanceOf",
            "Json",
            "JsonValue",
            "NonNegativeFloat",
            "NonNegativeInt",
            "PositiveFloat",
            "PositiveInt",
            "PrivateAttr",
            "ValidationInfo",
            "RootModel",
            "SerializeAsAny",
            "StrictBool",
            "StrictFloat",
            "StrictInt",
            "StrictStr",
            "TypeAdapter",
            "ValidationError",
            "field_serializer",
            "field_validator",
            "model_serializer",
            "model_validator",
        }
    ),
    "pydantic.dataclasses": frozenset({"dataclass"}),
    "pydantic.json_schema": frozenset({"GenerateJsonSchema", "JsonSchemaValue"}),
    "re": frozenset(
        {
            "A",
            "ASCII",
            "DOTALL",
            "I",
            "IGNORECASE",
            "M",
            "MULTILINE",
            "Match",
            "NOFLAG",
            "Pattern",
            "S",
            "U",
            "UNICODE",
            "VERBOSE",
            "X",
            "compile",
            "error",
            "escape",
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
        }
    ),
    "tomllib": frozenset({"TOMLDecodeError", "loads"}),
    "typing": frozenset(
        {
            "Annotated",
            "Any",
            "Callable",
            "ClassVar",
            "Final",
            "Generic",
            "Literal",
            "Never",
            "NewType",
            "NotRequired",
            "Protocol",
            "Required",
            "Self",
            "TYPE_CHECKING",
            "TypeAlias",
            "TypeVar",
            "TypedDict",
            "Union",
            "assert_never",
            "cast",
            "get_args",
            "get_origin",
            "overload",
            "override",
            "runtime_checkable",
        }
    ),
    "types": frozenset({"MappingProxyType", "NoneType", "UnionType"}),
    "unicodedata": frozenset(
        {
            "category",
            "combining",
            "decimal",
            "digit",
            "east_asian_width",
            "is_normalized",
            "lookup",
            "name",
            "normalize",
            "numeric",
        }
    ),
}
PURE_BUILTINS = frozenset(
    {
        "BaseException",
        "Ellipsis",
        "False",
        "None",
        "NotImplemented",
        "True",
        "getattr",
        "hasattr",
        "Exception",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "EOFError",
        "IndexError",
        "KeyError",
        "LookupError",
        "NotImplementedError",
        "OverflowError",
        "RuntimeError",
        "StopIteration",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
        "abs",
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytes",
        "chr",
        "classmethod",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "object",
        "oct",
        "ord",
        "pow",
        "property",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "staticmethod",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "zip",
    }
)
PURE_MODEL_MEMBERS = frozenset(
    {
        "model_config",
        "model_construct",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "model_fields",
        "model_fields_set",
        "model_json_schema",
        "model_validate",
        "model_validate_json",
        "model_validate_strings",
    }
)
MODEL_ORIGINS = frozenset(
    {
        "pydantic.BaseModel",
        "pydantic.RootModel",
        "vs_core.api.Value",
        "vs_core.types.common.Value",
        "@local.types.common.Value",
        "@local.common.Value",
    }
)
PURE_VALUE_APIS = ("vs_evaluator_protocol.api", "vs_loop_state.api", "vs_prompts.api")
PURE_REQUEST_APIS = (
    "vs_agent.api.requests",
    "vs_async_ops.api.requests",
    "vs_evaluation.api.requests",
    "vs_faults.api.requests",
    "vs_github.api.requests",
    "vs_issue_tracker.api.requests",
    "vs_project.api.requests",
    "vs_runtime.api.requests",
    "vs_sandbox.api.requests",
    "vs_slurm.api.requests",
)
IMPLEMENTATIONS = frozenset({"docker", "modal", "slurm", "claude-code", "omnigent"})
BUILTINS = frozenset(vars(builtins)) - PURE_BUILTINS
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
        self.model_classes: dict[str, frozenset[str]] = {}
        self.model_super: list[str] = []
        self.aliases: dict[str, str] = {
            "getattr": "builtins.getattr",
            "hasattr": "builtins.hasattr",
        }

    def record(self, rule: str, subject: str) -> None:
        """Normalize identity and distinguish repeated identical sites."""
        symbol = ".".join(self.symbols) or "<module>"
        key = (symbol, rule, subject)
        self.counts[key] += 1
        self.violations.append(Violation(self.path, symbol, rule, subject, self.counts[key]))

    def visit_Import(self, node: ast.Import) -> None:
        """Approving an import requires an explicit pure namespace."""
        for alias in node.names:
            self.check_import(alias.name)
            self.aliases[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )
            if alias.name == "builtins":
                self.builtin_modules.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Review each imported export, including TYPE_CHECKING branches."""
        module = node.module or ""
        self.check_import(module, relative=bool(node.level))
        for alias in node.names:
            path = f"{module}.{alias.name}" if module else alias.name
            self.aliases[alias.asname or alias.name] = f"@local.{path}" if node.level else path
            if module in PURE_EXPORTS and not self.permitted_path(path):
                self.record("effect-path", path)
            if module == "builtins" and alias.name in BUILTINS:
                self.builtin_aliases.add(alias.asname or alias.name)
                self.record("builtin", alias.name)
            if alias.name == "wiring":
                self.record("implementation-import", path)

    def permitted_local(self, module: str) -> bool:
        """Local values obey tach boundaries; I/O value APIs get closure scans."""
        if module == "vs_core" or module.startswith(("@local.", "vs_core.")):
            return True
        if self.path.startswith(PURE_SCOPE):
            return False
        return (
            module == "vibesys"
            or module.startswith("vibesys.")
            or any(module == api or module.startswith(f"{api}.") for api in PURE_VALUE_APIS)
            or any(module == api or module.startswith(f"{api}.") for api in PURE_REQUEST_APIS)
            or (self.path.startswith("libs/") and f"/src/{module.partition('.')[0]}/" in self.path)
        )

    def permitted_path(self, path: str) -> bool:
        """Permit reviewed exports only, never module internals or new exports."""
        if any(part.startswith("__") for part in path.split(".")) and not path.startswith(
            "__future__."
        ):
            return False
        for origin in MODEL_ORIGINS:
            if path.startswith(f"{origin}."):
                return path.removeprefix(f"{origin}.") in PURE_MODEL_MEMBERS
        if path.startswith("@model."):
            name, _, member = path.removeprefix("@model.").partition(".")
            return not member or member in PURE_MODEL_MEMBERS | self.model_classes.get(
                name, frozenset()
            )
        if self.permitted_local(path):
            return True
        if path in PURE_EXPORTS:
            return True
        module, _, export = path.rpartition(".")
        return export in PURE_EXPORTS.get(module, ()) or (
            module == "builtins" and export in PURE_BUILTINS
        )

    def check_import(self, module: str, *, relative: bool = False) -> None:
        """Unreviewed external imports fail closed rather than needing a ban."""
        if (
            not relative
            and module not in PURE_EXPORTS
            and module != "builtins"
            and not self.permitted_local(module)
        ):
            self.record("io-import" if module.startswith("vs_") else "banned-import", module)
        if "wiring" in module.split("."):
            self.record("implementation-import", module)

    def resolve_path(self, node: ast.expr) -> str | None:
        """Normalize direct references and assignment aliases to import paths."""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and self.model_super
        ):
            return self.model_super[-1]
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.resolve_path(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def bind_alias(self, target: ast.expr, value: ast.expr) -> None:
        """Track reference copies through ordinary and destructured bindings."""
        path = self.resolve_path(value)
        if isinstance(target, ast.Name) and path:
            self.aliases[target.id] = path
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for item, expression in zip(target.elts, value.elts, strict=False):
                self.bind_alias(item, expression)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track copied module/export references without executing the source."""
        for target in node.targets:
            self.bind_alias(target, node.value)
        self.generic_visit(node)

    def scan_annotation(self, node: ast.expr) -> None:
        """Forward annotations are Python expressions evaluated by model libraries."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                expression = ast.parse(node.value, mode="eval")
            except SyntaxError:
                self.record("annotation-expression", node.value)
            else:
                self.visit(expression)
                self.scan_annotation(expression.body)
        elif isinstance(node, ast.Subscript):
            origin = self.resolve_path(node.value)
            if origin == "typing.Literal":
                return
            parameter = node.slice
            if origin == "typing.Annotated" and isinstance(parameter, ast.Tuple):
                parameter = parameter.elts[0]
            self.scan_annotation(parameter)
        elif isinstance(node, ast.Tuple):
            for item in node.elts:
                self.scan_annotation(item)
        elif isinstance(node, ast.BinOp):
            self.scan_annotation(node.left)
            self.scan_annotation(node.right)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Annotations cannot hide module references or executable expressions."""
        self.scan_annotation(node.annotation)
        if node.value:
            self.bind_alias(node.target, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """Expression-local bindings preserve their imported origin."""
        self.bind_alias(node.target, node.value)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Dynamic builtin lookup and effect references cannot escape via aliases."""
        if isinstance(node.ctx, ast.Load) and node.id in self.builtin_aliases:
            self.record("builtin", node.id)

    def visit_Call(self, node: ast.Call) -> None:
        """Dynamic lookup of imported namespaces needs the same path approval."""
        if self.resolve_path(node.func) == "pydantic.TypeAdapter":
            if node.args:
                self.scan_annotation(node.args[0])
            for keyword in node.keywords:
                if keyword.arg == "type":
                    self.scan_annotation(keyword.value)
        if node.args and (
            (isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"})
            or self.resolve_path(node.func) in {"builtins.getattr", "builtins.hasattr"}
        ):
            parent = self.resolve_path(node.args[0])
            if parent:
                attribute = node.args[1] if len(node.args) > 1 else None
                if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                    path = f"{parent}.{attribute.value}"
                    if not self.permitted_path(path):
                        self.record("effect-path", path)
                else:
                    self.record("effect-path", f"{parent}.<dynamic>")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Inspect every imported attribute path, including chained streams."""
        path = self.resolve_path(node)
        if path and not self.permitted_path(path):
            self.record("effect-path", path)
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
        """Model subclasses retain the pure-member limits of their imported base."""
        bases = [
            self.resolve_path(base.value if isinstance(base, ast.Subscript) else base)
            for base in node.bases
        ]
        model_base = next(
            (
                base
                for base in bases
                if base in MODEL_ORIGINS or (base and base.startswith("@model."))
            ),
            None,
        )
        if model_base:
            declared = set(self.model_classes.get(model_base.removeprefix("@model."), ()))
            for statement in node.body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    declared.add(statement.name)
                elif (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and isinstance(statement.annotation, ast.Subscript)
                    and self.resolve_path(statement.annotation.value) == "typing.ClassVar"
                ):
                    declared.add(statement.target.id)
            self.model_classes[node.name] = frozenset(declared)
            self.aliases[node.name] = f"@model.{node.name}"
            self.model_super.append(model_base)
        self._scope(node)
        if model_base:
            self.model_super.pop()

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
        f"pure waiver: {entry}" for entry in sorted(baseline) if entry.path.startswith("libs/")
    )
    if previous is not None:
        errors.extend(f"baseline growth: {entry}" for entry in sorted(baseline - previous))
    return tuple(errors)


def previous_baseline(root: Path, base_ref: str) -> frozenset[Violation] | None:
    """Require explicit valid merge-base context; allow only first introduction."""
    shallow = run_git(["rev-parse", "--is-shallow-repository"], cwd=root, text=True)
    shallow.check_returncode()
    resolved = run_git(["rev-parse", "--verify", base_ref], cwd=root, text=True)
    if base_ref.startswith("origin/") and (resolved.returncode or shallow.stdout.strip() == "true"):
        branch = base_ref.removeprefix("origin/")
        args = ["fetch", "--no-tags"]
        if shallow.stdout.strip() == "true":
            args.append("--unshallow")
        args.extend(["origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"])
        fetched = run_git(args, cwd=root, text=True)
        fetched.check_returncode()
    else:
        resolved.check_returncode()
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
