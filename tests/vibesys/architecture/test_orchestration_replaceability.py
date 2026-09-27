"""A strategy under ``vibesys.orchestration.<strategy>`` must be replaceable on its own.

Replaceability means: a strategy is its folder + one registry line + its
prompt folder. Concretely:

  1. No strategy package imports another strategy package (peers never
     import each other).
  2. Nothing outside ``vibesys.orchestration`` imports a policy package,
     except the product plugin catalog.

Both checks are pure ``ast`` scans over ``src/vibesys`` so they stay cheap and
do not require importing the package under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_ORCHESTRATION = _SRC / "orchestration"

_ALLOWED_EXTERNAL_POLICY_IMPORTS = {"api/__init__.py", "plugin_catalog.py"}
_INFRASTRUCTURE_LIBRARIES = (
    "vs_agent",
    "vs_evaluator_protocol",
    "vs_project",
    "vs_sandbox",
)


def _strategy_names() -> set[str]:
    return {
        path.name
        for path in _ORCHESTRATION.iterdir()
        if path.is_dir() and (path / "plugin.py").is_file()
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
    prefix = "vibesys.orchestration."
    if not module_name.startswith(prefix):
        return None
    rest = module_name[len(prefix) :].split(".", 1)[0]
    return rest if rest in strategies else None


def test_no_strategy_package_imports_a_peer_strategy_package() -> None:
    strategies = _strategy_names()
    violations: list[str] = []
    for strategy in strategies:
        for path in (_ORCHESTRATION / strategy).rglob("*.py"):
            for module_name in _imported_module_names(path):
                other = _strategy_of(module_name, strategies)
                if other is not None and other != strategy:
                    violations.append(f"{path.relative_to(_SRC)} imports {module_name}")
    assert not violations, "strategy package imports a peer strategy: " + "; ".join(violations)


def test_strategy_packages_reach_infrastructure_only_through_runtime_api() -> None:
    """Keep product policy independent of concrete execution libraries."""
    violations = [
        f"{path.relative_to(_SRC)} imports {module_name}"
        for strategy in _strategy_names()
        for path in (_ORCHESTRATION / strategy).rglob("*.py")
        for module_name in _imported_module_names(path)
        if any(
            module_name == package or module_name.startswith(f"{package}.")
            for package in _INFRASTRUCTURE_LIBRARIES
        )
    ]
    assert not violations, (
        "strategy package bypasses vs_runtime.api for infrastructure: " + "; ".join(violations)
    )


def test_nothing_outside_orchestration_imports_a_strategy_package_except_catalog() -> None:
    strategies = _strategy_names()
    policy_roots = {_ORCHESTRATION / strategy for strategy in strategies}
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        if any(policy_root in path.parents for policy_root in policy_roots):
            continue  # inside policy packages: covered by the peer check above
        for module_name in _imported_module_names(path):
            if _strategy_of(module_name, strategies) is None:
                continue
            rel = str(path.relative_to(_SRC))
            if rel in _ALLOWED_EXTERNAL_POLICY_IMPORTS:
                continue
            violations.append(f"{rel} imports {module_name}")
    assert not violations, (
        "only product composition and its typed facade may import a policy package: "
        + "; ".join(violations)
    )
