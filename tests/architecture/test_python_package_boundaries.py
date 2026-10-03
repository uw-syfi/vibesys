from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DYNAMIC_LOADERS = {"importlib.import_module", "importlib.util.find_spec"}


def test_old_nested_server_package_is_absent() -> None:
    legacy_package = "vibesys.server"
    assert importlib.util.find_spec(legacy_package) is None


def _dynamic_import_aliases(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    module_aliases: dict[str, str] = {}
    loader_aliases = {"__import__"}
    for imported in ast.walk(tree):
        if isinstance(imported, ast.Import):
            for alias in imported.names:
                if alias.name in {"importlib", "importlib.util"}:
                    local_name = alias.asname or alias.name.split(".", 1)[0]
                    module_aliases[local_name] = alias.name if alias.asname else local_name
        elif isinstance(imported, ast.ImportFrom) and imported.module in {
            "importlib",
            "importlib.util",
        }:
            for alias in imported.names:
                local_name = alias.asname or alias.name
                qualified = f"{imported.module}.{alias.name}"
                if qualified in _DYNAMIC_LOADERS:
                    loader_aliases.add(local_name)
                elif qualified == "importlib.util":
                    module_aliases[local_name] = qualified
    return module_aliases, loader_aliases


def _qualified_name(expression: ast.expr, module_aliases: dict[str, str]) -> str | None:
    if isinstance(expression, ast.Name):
        return module_aliases.get(expression.id, expression.id)
    if isinstance(expression, ast.Attribute):
        owner = _qualified_name(expression.value, module_aliases)
        return f"{owner}.{expression.attr}" if owner is not None else None
    return None


def _dynamically_loads_vibesys(tree: ast.AST) -> bool:
    module_aliases, loader_aliases = _dynamic_import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _qualified_name(node.func, module_aliases)
        if target not in {*loader_aliases, *_DYNAMIC_LOADERS}:
            continue
        arguments = (*node.args, *(keyword.value for keyword in node.keywords))
        if any(
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and (argument.value == "vibesys" or argument.value.startswith("vibesys."))
            for argument in arguments
        ):
            return True
    return False


def test_dynamic_product_discovery_guard_recognizes_supported_import_forms() -> None:
    discovery_forms = (
        'import importlib.util\nimportlib.util.find_spec("vibesys")',
        'from importlib.util import find_spec\nfind_spec(name="vibesys.api")',
        'import importlib as loader\nloader.import_module(name="vibesys")',
        'from importlib import import_module as load\nload("vibesys.api")',
        '__import__("vibesys")',
    )

    assert all(_dynamically_loads_vibesys(ast.parse(source)) for source in discovery_forms)
    assert not _dynamically_loads_vibesys(ast.parse('registry.find_spec("vibesys")'))


def test_lower_libraries_do_not_discover_the_vibesys_product_dynamically() -> None:
    """Library-to-product dependencies must not bypass the static module graph."""
    violations: list[str] = []
    libraries = _REPOSITORY_ROOT / "libs"
    for path in libraries.glob("*/src/**/*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if _dynamically_loads_vibesys(tree):
            violations.append(str(path.relative_to(_REPOSITORY_ROOT)))

    assert not violations, "lower libraries dynamically discover VibeSys: " + ", ".join(
        sorted(set(violations))
    )
