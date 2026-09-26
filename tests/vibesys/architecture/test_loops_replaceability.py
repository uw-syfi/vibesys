"""A strategy under ``vibesys.loops.<strategy>`` must be replaceable on its own.

Replaceability means: a strategy is its folder + one registry line + its
prompt folder. Concretely:

  1. No strategy package imports another strategy package (peers never
     import each other; only ``vibesys.loops.registry`` imports all of them).
  2. Nothing outside ``vibesys.loops`` imports a strategy package directly;
     the only doorway in is ``vibesys.loops.registry``.

Both checks are pure ``ast`` scans over ``src/vibesys`` so they stay cheap and
do not require importing the package under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_LOOPS = _SRC / "loops"

# Known debt: real code that reaches a strategy package directly instead of
# going through vibesys.loops.registry. Keep this allowlist empty over time;
# each entry is a file path (relative to src/vibesys) that still needs to be
# rewired onto the registry.
_ALLOWED_EXTERNAL_STRATEGY_IMPORTS = {
    # server/packaging: the built-in evolve CLI option adapter reaches into
    # evolve's orchestration module directly for `resolve_openevolve_options`
    # instead of going through the registry. Tracked debt, not yet rewired.
    "api/evolve.py",
}


def _strategy_names() -> set[str]:
    return {
        path.name for path in _LOOPS.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
    }


def _imported_module_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _strategy_of(module_name: str, strategies: set[str]) -> str | None:
    """Return the strategy folder ``module_name`` refers to, if any."""
    prefix = "vibesys.loops."
    if not module_name.startswith(prefix):
        return None
    rest = module_name[len(prefix) :].split(".", 1)[0]
    return rest if rest in strategies else None


def test_no_strategy_package_imports_a_peer_strategy_package() -> None:
    strategies = _strategy_names()
    violations: list[str] = []
    for strategy in strategies:
        for path in (_LOOPS / strategy).rglob("*.py"):
            for module_name in _imported_module_names(path):
                other = _strategy_of(module_name, strategies)
                if other is not None and other != strategy:
                    violations.append(f"{path.relative_to(_SRC)} imports {module_name}")
    assert not violations, "strategy package imports a peer strategy: " + "; ".join(violations)


def test_nothing_outside_loops_imports_a_strategy_package_except_the_registry() -> None:
    strategies = _strategy_names()
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        if _LOOPS in path.parents:
            continue  # inside vibesys.loops itself: covered by the peer check above
        for module_name in _imported_module_names(path):
            if _strategy_of(module_name, strategies) is None:
                continue
            rel = str(path.relative_to(_SRC))
            if rel in _ALLOWED_EXTERNAL_STRATEGY_IMPORTS:
                continue
            violations.append(f"{rel} imports {module_name}")
    assert not violations, (
        "only vibesys.loops.registry may import a strategy package directly: "
        + "; ".join(violations)
    )
