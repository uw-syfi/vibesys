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

import pytest

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_ORCHESTRATION = _SRC / "orchestration"

_ALLOWED_EXTERNAL_POLICY_IMPORTS = {"api/evolve.py", "api/catalog.py"}
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


def _is_infrastructure_import(module_name: str) -> bool:
    # The evaluator's public protocol API contains pure data contracts, not
    # executors or transport mechanisms. Keep implementation imports forbidden.
    return module_name != "vs_evaluator_protocol.api" and any(
        module_name == package or module_name.startswith(f"{package}.")
        for package in _INFRASTRUCTURE_LIBRARIES
    )


@pytest.mark.parametrize(
    ("module_name", "forbidden"),
    [
        ("vs_evaluator_protocol.api", False),
        ("vs_evaluator_protocol", True),
        ("vs_evaluator_protocol.records", True),
        ("vs_evaluator_protocol.api.records", True),
        ("vs_agent.api", True),
        ("vs_project.api", True),
        ("vs_sandbox.api", True),
        ("vs_runtime.api", False),
    ],
)
def test_only_public_protocol_values_are_exempt_from_infrastructure_imports(
    module_name: str, *, forbidden: bool
) -> None:
    assert _is_infrastructure_import(module_name) is forbidden


def test_strategy_packages_reach_infrastructure_only_through_runtime_api() -> None:
    """Keep product policy independent of concrete execution libraries."""
    violations = [
        f"{path.relative_to(_SRC)} imports {module_name}"
        for strategy in _strategy_names()
        for path in (_ORCHESTRATION / strategy).rglob("*.py")
        for module_name in _imported_module_names(path)
        if _is_infrastructure_import(module_name)
    ]
    assert not violations, (
        "strategy package bypasses vs_runtime.api for infrastructure: " + "; ".join(violations)
    )


def test_orchestration_policy_never_imports_composition_only_runtime_api() -> None:
    """Keep concrete runtime construction below every orchestration policy module."""
    forbidden = "vs_runtime.api.infrastructure"
    violations = [
        f"{path.relative_to(_SRC)} imports {module_name}"
        for path in _ORCHESTRATION.rglob("*.py")
        for module_name in _imported_module_names(path)
        if module_name == forbidden or module_name.startswith(f"{forbidden}.")
    ]
    assert not violations, (
        "orchestration policy imports composition-only runtime API: " + "; ".join(violations)
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
