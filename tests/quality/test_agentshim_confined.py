"""Only ``vs_agent`` imports ``agentshim``.

``vs_agent`` owns the agent-CLI integration; every other package reaches it through
``vs_agent.api``. Ruff's ``TID251`` could say this, but its banned-API table is one table per
config file: a per-library table for ``libs/vs-agent`` would replace the repository's table
and drop its other bans, so the rule is checked here instead. The real end-to-end tier drives
the real library and may name it too.
"""

import ast
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_file, parse_source, python_files

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("src", "tests", "libs", "sdk", "support")
ALLOWED_PREFIXES = ("libs/vs-agent/", "tests/e2e/")
PACKAGE = "agentshim"


def imports_agentshim(tree: ast.AST) -> bool:
    """Whether the module imports ``agentshim`` or one of its submodules."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name == PACKAGE or name.startswith(f"{PACKAGE}.") for name in names):
            return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "import agentshim",
        "import os, agentshim.drivers as d",
        "from agentshim import Driver",
        "from agentshim.sessions import Session",
    ],
)
def test_the_detector_finds_an_import(source: str) -> None:
    assert imports_agentshim(parse_source(source))


@pytest.mark.parametrize(
    "source",
    [
        "from . import agentshim",
        "import agentshim_extras",
        "from vs_agent.api import AgentClient",
        "x = 'agentshim'",
    ],
)
def test_the_detector_ignores_other_names(source: str) -> None:
    assert not imports_agentshim(parse_source(source))


def test_only_vs_agent_imports_agentshim() -> None:
    offenders = [
        relative
        for root in SCANNED_ROOTS
        for path in python_files(REPO / root)
        if ".venv" not in path.parts
        and not (relative := path.relative_to(REPO).as_posix()).startswith(ALLOWED_PREFIXES)
        and imports_agentshim(parse_file(path))
    ]
    assert not offenders, f"only vs_agent may import agentshim; use vs_agent.api: {offenders}"
