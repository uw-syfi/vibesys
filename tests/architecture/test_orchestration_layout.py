"""The package tree declares strategy ownership and dependency direction."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

from vibesys.plugin_builtins import built_in_orchestrations
from vibesys.plugin_registration import OrchestrationRegistration

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_ORCHESTRATION = _SOURCE_ROOT / "vibesys" / "orchestration"
_SHARED_PACKAGES = ("domains", "hypothesis", "profile_focus", "steering", "prompts", "metrics")


def test_every_direct_orchestration_folder_registers_a_strategy() -> None:
    """A shared folder or an unregistered strategy cannot hide under orchestration."""
    registry = built_in_orchestrations()
    for folder in sorted(_ORCHESTRATION.iterdir()):
        if not folder.is_dir() or folder.name == "__pycache__":
            continue
        module = importlib.import_module(f"vibesys.orchestration.{folder.name}")
        registration = getattr(module, "REGISTRATION", None)
        assert isinstance(registration, OrchestrationRegistration), (
            f"{folder.relative_to(_REPOSITORY_ROOT)} must export a strategy REGISTRATION"
        )
        assert registry.resolve(registration.plugin.id) is registration, (
            f"{registration.plugin.id} must be registered in the built-in catalog"
        )
        assert registration.plugin.orchestrate.__module__.startswith(f"{module.__name__}."), (
            f"{folder.name} must own its registered orchestration strategy"
        )


def _orchestration_imports(source: str, package: str) -> list[str]:
    imports: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        imports.extend(
            name
            for name in names
            if name == "vibesys.orchestration" or name.startswith("vibesys.orchestration.")
        )
    return imports


@pytest.mark.parametrize(
    "source",
    [
        "import vibesys.orchestration.single",
        "from vibesys.orchestration import single",
        "from vibesys import orchestration",
        "from ..orchestration import single",
        "from .. import orchestration",
    ],
)
def test_dependency_guard_recognizes_absolute_and_relative_imports(source: str) -> None:
    assert _orchestration_imports(source, "vibesys.hypothesis")
    assert not _orchestration_imports("from vibesys import metrics", "vibesys.hypothesis")


def test_shared_siblings_never_import_orchestration() -> None:
    violations: list[str] = []
    for name in _SHARED_PACKAGES:
        location = _SOURCE_ROOT / "vibesys" / name
        paths = location.rglob("*.py") if location.is_dir() else [location.with_suffix(".py")]
        for path in sorted(paths):
            package = ".".join(path.parent.relative_to(_SOURCE_ROOT).parts)
            imported = _orchestration_imports(path.read_text(), package)
            violations.extend(
                f"{path.relative_to(_REPOSITORY_ROOT)}: {target}" for target in imported
            )
    assert not violations, "shared packages import orchestration:\n" + "\n".join(violations)


def test_tach_forbids_shared_sibling_edges_to_orchestration() -> None:
    config = tomllib.loads((_REPOSITORY_ROOT / "tach.toml").read_text())
    shared = tuple(f"vibesys.{name}" for name in _SHARED_PACKAGES)
    violations: list[str] = []
    for module in config["modules"]:
        source = module["path"]
        if not any(source == name or source.startswith(f"{name}.") for name in shared):
            continue
        violations.extend(
            f"{source} -> {target}"
            for target in module.get("depends_on", [])
            if target == "vibesys.orchestration" or target.startswith("vibesys.orchestration.")
        )
    declared = {module["path"] for module in config["modules"]}
    assert set(shared) <= declared, "every shared sibling must be declared as a tach module"
    assert not violations, "tach permits upward edges:\n" + "\n".join(violations)
