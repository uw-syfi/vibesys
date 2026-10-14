"""Every profiler kind is declared, packaged, prompted and honest about its driver tools.

Adding a profiler touches several places that nothing ties together at import
time: the ``ProfilerKind`` member, its ``ProfilerDefinition``, the bundled MCP
server under ``resources/profilers/<kind>/``, and the agent prompt
``profilers/<kind>.j2``. A missed one surfaces only when a run selects the
profiler. These checks fail CI naming the kind and the missing piece, and they
derive every list from the registry or the tree so a new profiler cannot slip
past them. The rule that every tool refuses undeclared arguments is
``tests/vibesys/loops/test_profiler_strict_arguments.py``.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import vibesys.prompts
from vibesys.config import BUNDLED_RESOURCES
from vibesys.orchestration.profilers import (
    PROFILER_DEFINITIONS,
    ProfilerDefinition,
    ProfilerKind,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

# ``auto`` and ``none`` are selection modes, not profilers.
_MODES = frozenset({ProfilerKind.AUTO, ProfilerKind.NONE})
_DEFINITIONS = tuple(sorted(PROFILER_DEFINITIONS.values(), key=lambda d: d.kind.value))
_PROMPTS = Path(vibesys.prompts.__file__).parent / "shared" / "profilers"


def _support_root() -> Path:
    root = BUNDLED_RESOURCES.directory("profilers")
    assert root is not None
    return root


@functools.cache
def _build(kind: ProfilerKind, withheld: frozenset[str]) -> FastMCP:
    """The server ``kind`` ships, loaded by path as the agent's interpreter runs it."""
    path = _support_root() / kind.value / "server.py"
    spec = importlib.util.spec_from_file_location(f"_registry_{kind.value}_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module.build_server(withheld=withheld) if withheld else module.build_server()


def _tool_names(kind: ProfilerKind, withheld: frozenset[str] = frozenset()) -> frozenset[str]:
    return frozenset(tool.name for tool in asyncio.run(_build(kind, withheld).list_tools()))


def test_every_profiler_kind_has_a_definition() -> None:
    missing = sorted(kind.value for kind in set(ProfilerKind) - _MODES - set(PROFILER_DEFINITIONS))

    assert not missing, f"ProfilerKind members with no ProfilerDefinition: {missing}"


@pytest.mark.parametrize("definition", _DEFINITIONS, ids=lambda d: d.kind.value)
def test_every_definition_ships_a_server_and_a_prompt(definition: ProfilerDefinition) -> None:
    server = _support_root() / definition.kind.value / "server.py"
    prompt = _PROMPTS / Path(definition.prompt_template).name

    assert server.is_file(), f"{definition.kind.value}: no MCP server at {server}"
    assert prompt.is_file(), f"{definition.kind.value}: no prompt at {prompt}"


def test_every_support_package_and_prompt_belongs_to_a_definition() -> None:
    """A package or prompt named for no profiler is never staged or rendered."""
    kinds = {definition.kind.value for definition in _DEFINITIONS}
    packages = {
        path.name
        for path in _support_root().iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    }
    prompts = {path.stem for path in _PROMPTS.glob("*.j2") if not path.name.startswith("_")}

    assert packages <= kinds, f"support packages with no definition: {sorted(packages - kinds)}"
    assert prompts <= kinds, f"profiler prompts with no definition: {sorted(prompts - kinds)}"


@pytest.mark.parametrize(
    "definition", [d for d in _DEFINITIONS if d.driver_tools], ids=lambda d: d.kind.value
)
def test_driver_tools_are_real_tools_the_server_can_withhold(
    definition: ProfilerDefinition,
) -> None:
    """``driver_tools`` names tools the server registers and drops when the run withholds them.

    A run whose editor has no GPU withholds them (#2185); a name the server does
    not register, or a server that ignores ``withheld``, would offer a tool
    that fails in every turn.
    """
    offered = _tool_names(definition.kind)
    withheld = _tool_names(definition.kind, definition.driver_tools)

    assert definition.driver_tools <= offered, sorted(definition.driver_tools - offered)
    assert not definition.driver_tools & withheld, sorted(definition.driver_tools & withheld)
    assert withheld == offered - definition.driver_tools
