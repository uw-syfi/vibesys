"""Application assembly is visible only to wiring and its integration tests."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN = ("vibesys", "headless", "server")


def test_tach_restricts_launch_to_application_wiring() -> None:
    config = tomllib.loads((_REPOSITORY_ROOT / "tach.toml").read_text())
    modules = {module["path"]: module for module in config["modules"]}

    assert set(modules["launch"]["visibility"]) == {"entrypoints", "tests", "scripts"}
    for name, module in modules.items():
        if name.split(".")[0] in _FORBIDDEN:
            assert "launch" not in module.get("depends_on", []), name


def test_core_and_frontends_do_not_import_launch() -> None:
    violations = []
    for package in _FORBIDDEN:
        for path in (_REPOSITORY_ROOT / "src" / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Import):
                    imported = (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported = (node.module or "",)
                else:
                    continue
                if any(name == "launch" or name.startswith("launch.") for name in imported):
                    violations.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{node.lineno}")
    assert violations == []
