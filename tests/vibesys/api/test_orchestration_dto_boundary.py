"""Canonical orchestration DTOs keep one public identity and dependency direction."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from framework.api import RunResult as InternalResult
from framework.api import RunStatus as InternalStatus
from framework.api import RunView as InternalView
from vibesys.api import ResumeRef, RunRequest, RunResult, RunStatus, RunView
from vibesys.api import contracts as api_contracts
from vibesys.orchestration.environment import AgentEnvironment
from vibesys.orchestration.request import ResumeRef as InternalResumeRef
from vibesys.orchestration.request import RunRequest as InternalRequest


def test_public_dtos_reexport_internal_classes() -> None:
    assert RunRequest is api_contracts.RunRequest is InternalRequest
    assert ResumeRef is api_contracts.ResumeRef is InternalResumeRef
    assert RunResult is api_contracts.RunResult is InternalResult
    assert RunStatus is api_contracts.RunStatus is InternalStatus
    assert RunView is api_contracts.RunView is InternalView
    assert api_contracts.AgentEnvironment is AgentEnvironment


def test_internal_dtos_do_not_import_public_api() -> None:
    script = (
        "import sys; "
        "import vibesys.orchestration.request, framework.api, "
        "vibesys.orchestration.environment; "
        "assert 'vibesys.api' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], check=True)  # noqa: S603  # LW-030002; The subprocess runs the current interpreter on a fixed script literal.


def test_internal_orchestration_and_policies_do_not_import_public_api() -> None:
    """The public facade depends on internal code, never the reverse."""
    root = Path(__file__).resolve().parents[3] / "src" / "vibesys"
    violations: list[str] = []
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
                    name and (name == "vibesys.api" or name.startswith("vibesys.api."))
                    for name in names
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "internal modules import vibesys.api: " + ", ".join(violations)
