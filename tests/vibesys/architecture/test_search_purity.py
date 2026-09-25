"""``vibesys.search`` stays pure: deterministic state transitions, no effects.

Rules (see the orchestration-simplify design brief):
  - No imports of vibesys.orchestration, vibesys.loops, vibesys.roles,
    vibesys.prompts (search answers questions and returns new state; it never
    drives agents, renders prompts, or touches RunContext).
  - No os / subprocess / pathlib I/O, no module-level ``random`` import (RNG
    state must live inside the persisted state value), no time/datetime
    clocks (search must be deterministic and resume-safe).

Both checks are pure ``ast`` scans, so they do not require importing
``vibesys.search`` (which would pull in its third-party dependencies).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_SEARCH = _SRC / "search"

_FORBIDDEN_PACKAGES = ("vibesys.orchestration", "vibesys.loops", "vibesys.roles", "vibesys.prompts")

# vibesys.agent_run dissolved into search/hypothesis, roles/, and loops/
# (except issue_board.py, a different lane's progress-board module). No
# search/ module re-exports from it any more.
_ALLOWED_AGENT_RUN_REEXPORTS: set[str] = set()

_FORBIDDEN_CLOCK_OR_IO_MODULES = ("os", "subprocess", "pathlib", "random", "time", "datetime")

# Known debt: population/ has not yet moved to fully-deterministic,
# state-carried RNG (design brief: "solved by state-as-data" for the
# OpenEvolve selector; PopulationSearch itself still seeds an unseeded
# `random.Random()` fallback in one path). Keep this allowlist empty over
# time as RNG state moves entirely into the persisted PopulationState.
_ALLOWED_IO_OR_RNG_IMPORTS = {
    "search/population/search.py": {"random"},
    "search/population/openevolve_selector.py": {"random", "pathlib"},
}


def _module_level_import_nodes(path: Path) -> list[tuple[ast.stmt, str]]:
    """Return (node, module_name) for every top-level (non-TYPE_CHECKING) import."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[ast.stmt, str]] = []
    for node in tree.body:
        if _is_type_checking_guard(node):
            continue
        if isinstance(node, ast.Import):
            out.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node, node.module))
    return out


def _is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return False


def test_search_imports_nothing_from_orchestration_loops_roles_or_prompts() -> None:
    violations: list[str] = []
    for path in _SEARCH.rglob("*.py"):
        for node, module_name in _module_level_import_nodes(path):
            if any(
                module_name == pkg or module_name.startswith(pkg + ".")
                for pkg in _FORBIDDEN_PACKAGES
            ):
                violations.append(f"{path.relative_to(_SRC)}:{node.lineno} imports {module_name}")
    assert not violations, "search/ imports a forbidden layer: " + "; ".join(violations)


def test_search_has_no_module_level_io_or_clock_imports() -> None:
    violations: list[str] = []
    for path in _SEARCH.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        for node, module_name in _module_level_import_nodes(path):
            if module_name.startswith("vibesys.agent_run"):
                if rel not in _ALLOWED_AGENT_RUN_REEXPORTS:
                    violations.append(
                        f"{rel}:{node.lineno} imports {module_name} (not an allowlisted re-export)"
                    )
                continue
            top = module_name.split(".", 1)[0]
            if top in _FORBIDDEN_CLOCK_OR_IO_MODULES:
                if top in _ALLOWED_IO_OR_RNG_IMPORTS.get(rel, set()):
                    continue
                violations.append(f"{rel}:{node.lineno} imports {module_name}")
    assert not violations, "search/ has an I/O, RNG, or clock import: " + "; ".join(violations)
