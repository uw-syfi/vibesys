"""Config loading, request validation, and default-request construction."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from vibesys.config import load_config as _load_config
from vibesys.evaluators.input_manifest import load_input_bundle

# Mirrors entrypoints/cli.py's `_DEFAULT_CONFIG_TEXT`: the minimal
# built-in config a run falls back to when a project has no `agent.toml`.
_DEFAULT_CONFIG_TEXT = '[model]\nname = "gpt-5.4"\n'

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.contracts import Config, ConfigurationDiagnostic, LoopKind, RunRequest
    from vs_project import Project


def load_config(path: Path, *, ignored_sections: frozenset[str] = frozenset()) -> Config:
    """Load and validate core configuration from a shared TOML file."""
    return _load_config(path, ignored_sections=ignored_sections)


def validate(request: RunRequest) -> list[ConfigurationDiagnostic]:
    """Return the diagnostics that make *request* unrunnable, if any.

    Checks the structural invariants that hold across every loop: the input
    bundle paths exist on disk. `RunRequest` is a frozen, `extra="forbid"`
    pydantic model, so field-shape errors already surface as a
    `pydantic.ValidationError` at construction time and never reach here.
    """
    from vibesys.errors import ConfigurationDiagnostic  # noqa: PLC0415

    diagnostics: list[ConfigurationDiagnostic] = []
    bundle = request.input_bundle
    if not bundle.root.exists():
        diagnostics.append(
            ConfigurationDiagnostic(
                code="missing_input",
                stage="input_validation",
                message=f"Input bundle root does not exist: {bundle.root}",
            )
        )
    if bundle.evaluator_path is not None and not bundle.evaluator_path.exists():
        diagnostics.append(
            ConfigurationDiagnostic(
                code="missing_evaluator",
                stage="input_validation",
                message=f"Evaluator path does not exist: {bundle.evaluator_path}",
            )
        )
    return diagnostics


def default_request(project: Project, loop: LoopKind) -> RunRequest:
    """Build the default `RunRequest` for *loop* in *project*.

    Sources `input_bundle` from the project's own directory and `config` from
    its `agent.toml`, if any, else the built-in defaults. Loop-specific fields
    are left at their `RunRequest` defaults.
    """
    from vibesys.api.contracts import Config, RunRequest  # noqa: PLC0415

    config_path = project.root / "agent.toml"
    config = (
        load_config(config_path)
        if config_path.is_file()
        else Config.model_validate(tomllib.loads(_DEFAULT_CONFIG_TEXT))
    )
    bundle = load_input_bundle(project.root)
    return RunRequest(
        project_root=project.root,
        loop=loop,
        config=config,
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=None,
    )
