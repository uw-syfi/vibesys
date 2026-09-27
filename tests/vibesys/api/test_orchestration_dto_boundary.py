"""Run DTOs keep one public identity and a downward dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

from vibesys.api import ResumeRef, RunRequest, RunResult, RunStatus, RunView
from vibesys.api import contracts as api_contracts


def test_public_dtos_have_one_identity() -> None:
    assert RunRequest is api_contracts.RunRequest
    assert ResumeRef is api_contracts.ResumeRef
    assert RunResult is api_contracts.RunResult
    assert RunStatus is api_contracts.RunStatus
    assert RunView is api_contracts.RunView


def test_orchestration_policy_imports_neither_public_api_nor_retired_hosts() -> None:
    """Policy depends on canonical run contracts, never API or retired homes."""
    root = Path(__file__).resolve().parents[3] / "src" / "vibesys"
    violations: list[str] = []
    forbidden = {
        "vibesys.api",
        "vibesys.orchestration.contracts",
        "vibesys.orchestration.request",
        "vibesys.orchestration.view",
    }
    for directory in (root / "orchestration", root / "loops"):
        for path in directory.rglob("*.py"):
            module = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(module):
                if isinstance(node, ast.Import):
                    names = (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names = (node.module,)
                else:
                    continue
                if any(
                    name
                    and any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
                    for name in names
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "policy imports a forbidden boundary: " + ", ".join(violations)
