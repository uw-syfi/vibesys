"""Flow code reaches the world only through ``ctx``: no clock, randomness, threads, I/O, or blind except."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scripts.flow_code import async_allowed_paths
from scripts.flow_code import flow_modules as shared_flow_modules
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

ROOT = Path(__file__).resolve().parents[2]
BANNED_MODULES = frozenset(
    {"asyncio", "time", "random", "threading", "subprocess", "os", "socket", "vs_sim"}
)


def _parsed(path: Path) -> ast.Module:
    return parse_source(path.read_text(encoding="utf-8"), filename=str(path))


def flow_modules() -> list[Path]:
    """Modules that import ``Flow`` plus the explicit flow packages."""
    return shared_flow_modules(ROOT)


def violations(tree: ast.Module) -> list[str]:
    """What a flow module does that flow code may not: banned imports and blind ``except``."""
    found: list[str] = []
    for node in walk_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            found += [
                f"line {node.lineno}: imports {name}"
                for name in names
                if name.split(".")[0] in BANNED_MODULES
            ]
        if isinstance(node, ast.ExceptHandler) and (
            node.type is None
            or (isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"})
        ):
            found.append(f"line {node.lineno}: blind except")
    return found


@pytest.mark.parametrize("path", flow_modules(), ids=lambda path: str(path.relative_to(ROOT)))
def test_flow_modules_use_only_ctx_for_the_outside_world(path: Path) -> None:
    assert violations(_parsed(path)) == []


@pytest.mark.parametrize(
    "source",
    [
        "import asyncio",
        "from time import sleep",
        "import os.path",
        "try:\n  pass\nexcept:\n  pass",
        "try:\n  pass\nexcept Exception:\n  pass",
    ],
)
def test_the_scan_catches_what_it_bans(source: str) -> None:
    assert violations(parse_source(source)) != []


def test_the_scan_allows_typed_except() -> None:
    assert violations(parse_source("try:\n  pass\nexcept ValueError:\n  pass")) == []


def test_the_purity_ratchet_and_this_scan_share_one_definition_of_flow_code() -> None:
    product = {path.relative_to(ROOT).as_posix() for path in flow_modules()}
    assert product <= async_allowed_paths(ROOT)
