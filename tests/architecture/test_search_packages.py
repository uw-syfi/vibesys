"""A search written as a flow keeps to its package: narrow imports in, a declared surface out.

A search package is a folder ``src/vibesys/orchestration/<name>/search/``. It reaches the core
library only through ``vs_core.api.search``, the facade a flow may use. The host-side policy
beside it (``core_policy/``) reads the wider ``vs_core.api`` because it renders prompts and
projects state for the host; that is the one place the wider facade is allowed under
``orchestration``. A search package declares its public surface in ``api.py``; everything else
in it is private (a leading underscore), and no module outside the package imports it.
"""

from __future__ import annotations

import ast
import importlib.util
import keyword
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

from vibesys.dynamic_roles import CORE_ROLES
from vibesys.orchestration.dynamic.search.api import ROLES, dynamic

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION = ROOT / "src" / "vibesys" / "orchestration"
SEARCH_API = "vs_core.api.search"
HOST_SIDE_POLICY = "core_policy"
"""The folder under an orchestration plugin that may read the whole ``vs_core.api``."""


def search_packages() -> list[Path]:
    """Every ``orchestration/<name>/search`` folder."""
    return sorted(path for path in ORCHESTRATION.glob("*/search") if path.is_dir())


def vs_core_imports(source: str) -> list[str]:
    """The ``vs_core`` modules ``source`` imports, spelled absolutely."""
    found: list[str] = []
    for node in walk_nodes(parse_source(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
    return [name for name in found if name == "vs_core" or name.startswith("vs_core.")]


def outside_api(imports: list[str]) -> list[str]:
    """The imports that are not the search facade."""
    return [name for name in imports if name != SEARCH_API]


def private_imports(source: str, package: str, current: str | None = None) -> list[str]:
    """The private modules (``package._x``) that ``source`` imports from ``package``."""
    found: list[str] = []
    if package.rpartition(".")[2] not in source:
        return found
    for node in walk_nodes(parse_source(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                if current is None:
                    continue
                base = importlib.util.resolve_name("." * node.level + base, current)
            found.append(base)
            found.extend(f"{base}.{alias.name}" for alias in node.names)
    prefix = f"{package}."
    modules = {
        f"{prefix}{name[len(prefix) :].split('.')[0]}"
        for name in found
        if name.startswith(prefix) and name[len(prefix) :].startswith("_")
    }
    return sorted(modules)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_repository_has_a_search_package() -> None:
    assert ORCHESTRATION / "dynamic" / "search" in search_packages()


@pytest.mark.parametrize("package", search_packages(), ids=lambda path: path.parent.name)
def test_a_search_package_imports_vs_core_only_through_the_search_facade(package: Path) -> None:
    violations = {
        _relative(path): outside_api(vs_core_imports(path.read_text("utf-8")))
        for path in sorted(package.rglob("*.py"))
    }
    assert not {name: found for name, found in violations.items() if found}


@pytest.mark.parametrize("package", search_packages(), ids=lambda path: path.parent.name)
def test_a_search_package_declares_its_public_surface(package: Path) -> None:
    api = package / "api.py"
    assert api.is_file(), f"{_relative(package)} needs an api.py"
    tree = parse_source(api.read_text("utf-8"))
    names = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "__all__" in names, f"{_relative(api)} must declare __all__"


@pytest.mark.parametrize("package", search_packages(), ids=lambda path: path.parent.name)
def test_nothing_outside_a_search_package_imports_its_private_modules(package: Path) -> None:
    dotted = ".".join(package.relative_to(ROOT / "src").parts)
    violations: dict[str, list[str]] = {}
    for base in (ROOT / "src", ROOT / "tests"):
        for path in sorted(base.rglob("*.py")):
            if path.is_relative_to(package):
                continue
            parts = path.relative_to(base).with_suffix("").parts
            current = ".".join(parts[:-1])
            found = private_imports(path.read_text("utf-8"), dotted, current)
            if found:
                violations[_relative(path)] = found
    assert not violations


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from vibesys.orchestration.dynamic.search import _roles", ["x._roles"]),
        ("from vibesys.orchestration.dynamic.search._roles import PLANNER", ["x._roles"]),
        ("import vibesys.orchestration.dynamic.search._search", ["x._search"]),
        ("from vibesys.orchestration.dynamic.search import api", []),
        ("from vibesys.orchestration.dynamic.search.api import dynamic", []),
    ],
)
def test_the_private_import_scan_recognizes_each_import_form(
    source: str, expected: list[str]
) -> None:
    package = "vibesys.orchestration.dynamic.search"
    found = [name.replace(package, "x") for name in private_imports(source, package)]
    assert found == expected


def test_the_private_import_scan_resolves_relative_imports() -> None:
    package = "vibesys.orchestration.dynamic.search"
    assert private_imports("from .search import _roles", package, "vibesys.orchestration.dynamic")
    assert not private_imports("from .search import api", package, "vibesys.orchestration.dynamic")


@given(
    name=st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True).filter(
        lambda text: text.isidentifier() and not keyword.iskeyword(text)
    ),
    private=st.booleans(),
)
def test_only_underscore_modules_count_as_private(*, name: str, private: bool) -> None:
    module = f"_{name}" if private else name
    found = private_imports(f"from pkg.search import {module}", "pkg.search")
    assert bool(found) == private


@given(
    modules=st.lists(
        st.sampled_from(["vs_core.api", "vs_core.api.search", "vs_core", "vs_core.flows", "json"]),
        max_size=6,
    )
)
def test_the_facade_scan_flags_exactly_the_wider_vs_core_imports(modules: list[str]) -> None:
    source = "\n".join(f"import {module}" for module in modules)
    flagged = outside_api(vs_core_imports(source))
    assert sorted(flagged) == sorted(
        module for module in modules if module.startswith("vs_core") and module != SEARCH_API
    )


def test_every_role_the_search_declares_has_its_agent_role_in_the_host() -> None:
    """The ids the search asks and the roles the host declares cannot drift apart."""
    asked = sorted(dynamic.ids.role_id(role.name).root for role in ROLES)
    assert asked == sorted(role.id for role in CORE_ROLES)
    assert all(role_id.startswith("dynamic-") for role_id in asked)
