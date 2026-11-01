"""The prove-or-block layer of run recovery stays deleted (#3475).

A run that cannot tell what an interruption left behind runs the step again, and
``clear`` makes that safe (``docs/contributing/run-recovery.md``). The layer that proved or
blocked instead (recovery barriers, inspections, receipts, effect fences, the lease and the
fence) is deleted module by module. Each deleting pull request adds what it removed to
``DELETED_MODULES`` or ``DELETED_SYMBOLS`` below, one line each, so the layer cannot come back
under the same name. Unenforced remainder: a new mechanism under a different name that does
the same job; review applies the rule "no result means run again".
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

DELETED_MODULES: tuple[str, ...] = (
    # The keyed-replay driver and its pure half (#3492, #3475 part 7a).
    "vibesys.serial_replay",
    "vs_core.api.replay",
    "vs_core.flows._replay_accounting",
    "vs_core.flows._replay_archive",
    "vs_core.flows._replay_causality",
    "vs_core.flows._replay_controls",
    "vs_core.flows._replay_history",
    "vs_core.flows._replay_identity",
    "vs_core.flows._replay_iterations",
    "vs_core.flows._replay_physical",
    "vs_core.flows._replay_resources",
    "vs_core.flows._replay_run",
    "vs_core.flows._replay_schema",
    "vs_core.flows._replay_sources",
    "vs_core.flows._replay_turns",
    "vs_core.flows._replay_writers",
    "vs_runtime._replay_controls",
    "vs_runtime._replay_evaluation",
    "vs_runtime._replay_evaluation_jobs",
    "vs_runtime._replay_evidence",
    "vs_runtime._replay_executors",
    "vs_runtime._replay_fake",
    "vs_runtime._replay_launch",
    "vs_runtime._replay_operations",
    "vs_runtime._replay_owner",
    "vs_runtime._replay_prompts",
    "vs_runtime._replay_run",
    "vs_runtime._replay_runtime",
    "vs_runtime._replay_sessions",
    "vs_runtime._replay_tools",
    "vs_runtime._replay_wiring",
    "vs_runtime._replay_workspaces",
)
"""Dotted module paths that no file under ``src/`` or ``libs/*/src`` may provide, as a module or
as a package (a listed ``a.b`` also forbids ``a.b.c``)."""

DELETED_SYMBOLS: tuple[str, ...] = ("ReplayUnknown",)
"""Names that no file under ``src/`` or ``libs/*/src`` may define or import."""

_ROOT = Path(__file__).resolve().parents[2]
_RULE = "docs/contributing/run-recovery.md, 'Keeping the deleted modules from coming back'"


def module_at(path: PurePosixPath) -> str | None:
    """The dotted module the product file at repository path *path* provides, else None.

    Product code is ``src/<package>/...`` and ``libs/<library>/src/<package>/...``; a package's
    ``__init__.py`` provides the package.
    """
    parts = path.with_suffix("").parts
    if parts[:1] == ("src",):
        below = parts[1:]
    elif parts[:1] == ("libs",) and parts[2:3] == ("src",):
        below = parts[3:]
    else:
        return None
    return ".".join(below[:-1] if below[-1:] == ("__init__",) else below) or None


def deleted_modules_present(
    modules: dict[str, str], deleted: tuple[str, ...] = DELETED_MODULES
) -> list[str]:
    """One message per file of *modules* (path to dotted module) that provides a deleted module."""
    return [
        f"{path}: provides `{name}`, which the prove-or-block cleanup deleted ({_RULE}). Do not"
        " bring it back; if the concept is wanted again, remove the name from DELETED_MODULES"
        " in the pull request that argues for it."
        for path, name in sorted(modules.items())
        for gone in deleted
        if name == gone or name.startswith(f"{gone}.")
    ]


def deleted_symbols_defined(
    path: str, source: str, deleted: tuple[str, ...] = DELETED_SYMBOLS
) -> list[str]:
    """One message per line of *source* that defines or imports a deleted name."""
    names = frozenset(deleted)
    found: list[tuple[int, str, str]] = []
    for node in walk_nodes(parse_source(source, path)):
        match node:
            case ast.ClassDef() | ast.FunctionDef() | ast.AsyncFunctionDef():
                found.append((node.lineno, node.name, "defines"))
            case ast.TypeAlias():
                found.append((node.lineno, node.name.id, "defines"))
            case ast.Assign():
                found.extend(
                    (node.lineno, target.id, "defines")
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )
            case ast.AnnAssign(target=ast.Name(id=name)):
                found.append((node.lineno, name, "defines"))
            case ast.Import() | ast.ImportFrom():
                for alias in node.names:
                    found.append((node.lineno, alias.name.rsplit(".", 1)[-1], "imports"))
                    if alias.asname is not None:
                        found.append((node.lineno, alias.asname, "imports"))
    return [
        f"{path}:{line}: {how} `{name}`, which the prove-or-block cleanup deleted ({_RULE}). Do"
        " not bring it back; if the concept is wanted again, remove the name from DELETED_SYMBOLS"
        " in the pull request that argues for it."
        for line, name, how in sorted(found)
        if name in names
    ]


def _product_files(root: Path) -> dict[str, Path]:
    """Each product source file, by its repository path."""
    return {
        str(path.relative_to(root)): path
        for base in (root / "src", *sorted((root / "libs").glob("*/src")))
        for path in sorted(base.rglob("*.py"))
    }


def test_no_deleted_module_is_provided_by_the_product() -> None:
    modules = {
        path: module
        for path in _product_files(_ROOT)
        if (module := module_at(PurePosixPath(path))) is not None
    }
    violations = deleted_modules_present(modules)
    assert not violations, "\n".join(violations)


def test_no_deleted_name_is_defined_or_imported_by_the_product() -> None:
    violations = [
        message
        for path, location in _product_files(_ROOT).items()
        if any(name in (text := location.read_text(encoding="utf-8")) for name in DELETED_SYMBOLS)
        for message in deleted_symbols_defined(path, text)
    ]
    assert not violations, "\n".join(violations)


def test_every_listed_name_is_a_plain_dotted_path_or_identifier() -> None:
    assert len(set(DELETED_MODULES)) == len(DELETED_MODULES)
    assert len(set(DELETED_SYMBOLS)) == len(DELETED_SYMBOLS)
    assert all(part.isidentifier() for name in DELETED_MODULES for part in name.split(".")), (
        "DELETED_MODULES holds dotted module paths"
    )
    assert all(name.isidentifier() for name in DELETED_SYMBOLS), "DELETED_SYMBOLS holds names"


def _layout(module: str, *, package: bool) -> str:
    """The repository path at which a file would provide *module*."""
    parts = module.split(".")
    leaf = [*parts, "__init__"] if package else parts
    base = "src" if parts[0] == "vibesys" else f"libs/{parts[0].replace('_', '-')}/src"
    return f"{base}/{'/'.join(leaf)}.py"


@given(module=st.sampled_from(DELETED_MODULES), package=st.booleans())
def test_every_deleted_module_fails_the_guard_wherever_it_is_brought_back(
    *, module: str, package: bool
) -> None:
    path = _layout(module, package=package)
    provided = module_at(PurePosixPath(path))
    assert provided == module
    assert len(deleted_modules_present({path: provided})) == 1


@given(st.sampled_from(DELETED_MODULES), st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True))
def test_a_module_below_a_deleted_package_fails_the_guard(module: str, child: str) -> None:
    assert len(deleted_modules_present({"p.py": f"{module}.{child}"})) == 1


@given(
    st.sampled_from(DELETED_MODULES),
    st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True),
)
def test_a_neighbour_that_only_shares_a_prefix_passes_the_guard(module: str, other: str) -> None:
    assert deleted_modules_present({"p.py": f"{module}_x9"}) == []
    assert deleted_modules_present({"p.py": f"zz_{other}.{module}"}) == []


@pytest.mark.parametrize(
    "source",
    [
        "class Gone: ...",
        "def Gone(): ...",
        "async def Gone(): ...",
        "Gone = 1",
        "Gone: int = 1",
        "type Gone = int",
        "from vs_core.api import Gone",
        "from vs_core.api import Other as Gone",
        "from vs_core.api import Gone as Other",
        "import vs_core.api.Gone",
        "import vs_core as Gone",
        "class Box:\n    Gone = 1",
        "if True:\n    def Gone(): ...",
    ],
)
def test_the_scan_sees_every_way_to_define_or_import_a_name(source: str) -> None:
    assert len(deleted_symbols_defined("p.py", source, deleted=("Gone",))) >= 1


@pytest.mark.parametrize(
    "source",
    [
        "class Other: ...",
        "x = Gone  # a use needs an import or a definition elsewhere",
        "'''Gone is mentioned in prose.'''",
        "from vs_core.api import Other",
        "value = other.Gone",
        "def f(Gone2): ...",
    ],
)
def test_the_scan_leaves_a_mention_or_a_use_alone(source: str) -> None:
    assert deleted_symbols_defined("p.py", source, deleted=("Gone",)) == []


def test_a_failure_names_the_file_the_line_and_the_fix() -> None:
    [module] = deleted_modules_present(
        {"libs/vs-core/src/vs_core/gone.py": "vs_core.gone"}, deleted=("vs_core.gone",)
    )
    [symbol] = deleted_symbols_defined("src/a.py", "x = 1\nclass Gone: ...", deleted=("Gone",))
    assert module.startswith("libs/vs-core/src/vs_core/gone.py: provides `vs_core.gone`")
    assert "DELETED_MODULES" in module
    assert symbol.startswith("src/a.py:2: defines `Gone`")
    assert "DELETED_SYMBOLS" in symbol


def test_a_package_init_provides_its_package_and_other_paths_provide_nothing() -> None:
    assert module_at(PurePosixPath("libs/vs-core/src/vs_core/api/__init__.py")) == "vs_core.api"
    assert module_at(PurePosixPath("libs/vs-core/src/vs_core/_a.py")) == "vs_core._a"
    assert module_at(PurePosixPath("src/vibesys/_a.py")) == "vibesys._a"
    assert module_at(PurePosixPath("libs/vs-core/tests/test_a.py")) is None
    assert module_at(PurePosixPath("tests/architecture/test_a.py")) is None
