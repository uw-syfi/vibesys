"""Tests for ``vibesys.loops.roles`` -- the advertised loop-to-agent-roles contract.

Rather than restating the registry, scan each loop's source for invoked role
names. The agent loop binds named handles, so its handle declarations must also
match the handles used at phase call sites. A role call or binding that drifts
from the advertised set fails this test.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import vibesys.loops.agent as agent_package
import vibesys.loops.evolve as evolve_package
import vibesys.loops.plain as plain_package
from vibesys.loops.roles import EXPECTED_AGENT_ROLES

if TYPE_CHECKING:
    from types import ModuleType

_LOOP_PACKAGES: dict[str, ModuleType] = {
    "agent": agent_package,
    "plain": plain_package,
    "evolve": evolve_package,
}


def _handle_bindings(package_dir: Path) -> dict[str, str]:
    """Map built-in agent-handle field names to their declared role IDs."""
    source = ast.parse((package_dir / "roles.py").read_text())
    bindings: dict[str, str] = {}
    for node in ast.walk(source):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "cls":
            continue
        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg is not None
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "SharedAgentHandle"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                bindings[keyword.arg] = value.args[0].value
    return bindings


def _invoked_roles(package: ModuleType) -> set[str]:
    """Every agent role ``package``'s source actually invokes."""
    package_dir = Path(inspect.getfile(package)).parent
    roles: set[str] = set()
    handle_uses: set[str] = set()
    for source_path in package_dir.rglob("*.py"):
        source = ast.parse(source_path.read_text())
        for node in ast.walk(source):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "invoke_profiler":
                roles.add("profiler")
            for keyword in node.keywords:
                if (
                    keyword.arg == "kind"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    roles.add(keyword.value.value)
                if (
                    keyword.arg == "agent"
                    and isinstance(keyword.value, ast.Attribute)
                    and (
                        (
                            isinstance(keyword.value.value, ast.Name)
                            and keyword.value.value.id == "agents"
                        )
                        or (
                            isinstance(keyword.value.value, ast.Attribute)
                            and keyword.value.value.attr == "agents"
                        )
                    )
                ):
                    handle_uses.add(keyword.value.attr)
    if package is agent_package:
        bindings = _handle_bindings(package_dir)
        assert handle_uses == set(bindings), "agent handle declarations and phase uses differ"
        roles.update(bindings[field] for field in handle_uses)
    return roles


def test_registry_matches_the_invoke_sites_for_every_loop() -> None:
    """``EXPECTED_AGENT_ROLES`` must name exactly the roles each loop can invoke."""
    for loop_kind, package in _LOOP_PACKAGES.items():
        assert _invoked_roles(package) == set(EXPECTED_AGENT_ROLES[loop_kind]), loop_kind
