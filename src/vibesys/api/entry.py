"""Config loading, request validation, and default-request construction."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from vibesys.config import load_config as _load_config
from vibesys.evaluators.input_manifest import load_input_bundle

# Mirrors entrypoints/headless.py's `_DEFAULT_CONFIG_TEXT`: the minimal
# built-in config a run falls back to when a project has no `agent.toml`.
_DEFAULT_CONFIG_TEXT = '[model]\nname = "gpt-5.4"\n'

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.contracts import Config, ConfigurationDiagnostic, LoopKind, RunRequest
    from vs_project import Project


def load_config(path: Path, *, ignored_sections: frozenset[str] = frozenset()) -> Config:
    """Load and validate core configuration from a shared TOML file.

    Delegates directly to `vibesys.config.load_config`; no wave-2 work needed.
    """
    return _load_config(path, ignored_sections=ignored_sections)


def validate(request: RunRequest) -> list[ConfigurationDiagnostic]:
    """Return the diagnostics that make *request* unrunnable, if any.

    Wave 2 covers only the structural checks that hold across every loop
    (input bundle paths actually exist on disk); `RunRequest` itself is a
    frozen, `extra="forbid"` pydantic model, so field-shape errors already
    surface as a `pydantic.ValidationError` at construction time and never
    reach this function.

    TODO(wave-3): fold in the semantic checks `vibesys.context
    .create_run_context` performs today (profiler/run-environment
    compatibility, resume/task mismatches, ...). That function raises
    `ConfigurationError` for the first diagnostic it finds instead of
    collecting every diagnostic; this entry point needs the collecting
    variant so callers can report every problem at once.
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

    So a chat "launch" action doesn't reassemble config by hand. Sources
    `input_bundle` from the project's own directory
    (`vibesys.evaluators.input_manifest.load_input_bundle`) and `config` from
    its `agent.toml`, if any, else the same built-in defaults
    `entrypoints/headless.py` falls back to.

    TODO(wave-3): restore the project's persisted default `RunConfiguration`
    (budgets, backend, skills, ...) the way
    `entrypoints/headless.py::_restore_project_resume_cli_args` does for a
    resume, instead of leaving every loop-specific field at its `RunRequest`
    default.
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
