"""Only ``vs_agent`` imports ``agentshim``.

``vs_agent`` owns the agent-CLI integration; every other package reaches it through
``vs_agent.api``. Ruff's ``TID251`` could say this, but its banned-API table is one table per
config file: a per-library table for ``libs/vs-agent`` would replace the repository's table
and drop its other bans, so the rule is checked here instead. The real end-to-end tier drives
the real library and may name it too.

Every Python file in the repository is scanned (hidden directories, ``node_modules`` and
``__pycache__`` aside), so scripts, packaging, resources, examples and the root
``conftest.py`` are held to the rule too. An import by a literal name through
``importlib.import_module`` or ``__import__`` counts like an import statement.
"""

import ast
import os
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
# Directories that hold no source of this repository's own.
SKIPPED_DIRECTORIES = frozenset({"node_modules", "__pycache__"})
ALLOWED_PREFIXES = ("libs/vs-agent/", "tests/e2e/")
PACKAGE = "agentshim"


def _dynamic_import_name(call: ast.Call) -> str | None:
    """The literal module name ``importlib.import_module`` or ``__import__`` is called with."""
    func = call.func
    named = (isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}) or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )
    if not named or not call.args:
        return None
    first = call.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    if isinstance(node, ast.Call) and (name := _dynamic_import_name(node)) is not None:
        return [name]
    return []


def imports_agentshim(tree: ast.AST) -> bool:
    """Whether the module imports ``agentshim`` or one of its submodules."""
    return any(
        name == PACKAGE or name.startswith(f"{PACKAGE}.")
        for node in ast.walk(tree)
        for name in _imported_names(node)
    )


def repository_python_files(root: Path) -> list[Path]:
    """Every ``*.py`` under ``root``, outside hidden and ``SKIPPED_DIRECTORIES`` directories."""
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not name.startswith(".") and name not in SKIPPED_DIRECTORIES
        )
        found.extend(Path(directory) / name for name in sorted(files) if name.endswith(".py"))
    return found


@pytest.mark.parametrize(
    "source",
    [
        "import agentshim",
        "import os, agentshim.drivers as d",
        "from agentshim import Driver",
        "from agentshim.sessions import Session",
        "import importlib\nimportlib.import_module('agentshim')",
        "from importlib import import_module\nimport_module('agentshim.drivers')",
        "__import__('agentshim')",
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
        "import importlib\nimportlib.import_module('agentshim_extras')",
        "import importlib\nimportlib.import_module(name)",
    ],
)
def test_the_detector_ignores_other_names(source: str) -> None:
    assert not imports_agentshim(parse_source(source))


def test_the_scan_reaches_every_top_level_source_directory(tmp_path: Path) -> None:
    planted = [
        "scripts/tool.py",
        "packaging/build.py",
        "resources/evaluators/run.py",
        "examples/task/bench.py",
        "conftest.py",
    ]
    for relative in [*planted, ".venv/lib/site.py", "clients/node_modules/gyp/x.py"]:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")

    found = {path.relative_to(tmp_path).as_posix() for path in repository_python_files(tmp_path)}

    assert found == set(planted)


def test_only_vs_agent_imports_agentshim() -> None:
    offenders = [
        relative
        for path in repository_python_files(REPO)
        if not (relative := path.relative_to(REPO).as_posix()).startswith(ALLOWED_PREFIXES)
        and PACKAGE in (source := path.read_text(encoding="utf-8"))
        and imports_agentshim(parse_source(source, relative))
    ]
    assert not offenders, f"only vs_agent may import agentshim; use vs_agent.api: {offenders}"
