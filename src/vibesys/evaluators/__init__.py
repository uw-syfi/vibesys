"""VibeSys selection of bundled evaluator packages and sandbox tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.config import BUNDLED_RESOURCES
from vs_runtime.api.infrastructure import (
    PACKAGE_ROOT_TOKEN,
    TOOL_TOKEN_PREFIX,
    EvaluatorPackageError,
    EvaluatorPackageMetadata,
    EvaluatorPackageNotFoundError,
    EvaluatorPackageRequirement,
    ResolvedEvaluatorPackage,
)
from vs_runtime.api.infrastructure import (
    resolve_evaluator_package as _resolve_evaluator_package,
)
from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN, PYTHON_TOKEN
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    cargo_install_argv,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
    tool_path_replacements,
    tool_spec_digest,
    tool_token,
)

if TYPE_CHECKING:
    from pathlib import Path

_MISSING_RESOURCES_MESSAGE = (
    "VibeSys evaluator package resources are not available; install a complete "
    "VibeSys distribution or pass packages_root"
)


def resolve_evaluator_package(
    requirement: EvaluatorPackageRequirement,
    *,
    packages_root: Path | None = None,
) -> ResolvedEvaluatorPackage:
    """Resolve an exact package from an explicit or bundled collection."""
    root = packages_root or BUNDLED_RESOURCES.directory("evaluators")
    if root is None:
        raise EvaluatorPackageNotFoundError(_MISSING_RESOURCES_MESSAGE)
    return _resolve_evaluator_package(root, requirement)


__all__ = [
    "PACKAGE_ROOT_TOKEN",
    "PROJECT_ROOT_TOKEN",
    "PYTHON_TOKEN",
    "TOOL_TOKEN_PREFIX",
    "CargoGitToolSpec",
    "EvaluatorPackageError",
    "EvaluatorPackageMetadata",
    "EvaluatorPackageNotFoundError",
    "EvaluatorPackageRequirement",
    "EvaluatorToolError",
    "EvaluatorToolLifecycleHooks",
    "ResolvedEvaluatorPackage",
    "cargo_install_argv",
    "evaluator_tools_install_command",
    "prepare_evaluator_tools",
    "resolve_evaluator_package",
    "tool_install_root",
    "tool_path_replacements",
    "tool_spec_digest",
    "tool_token",
]
