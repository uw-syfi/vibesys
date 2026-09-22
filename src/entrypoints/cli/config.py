"""Agent config loading, skill selection, and resume-time config restoration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.constants import _DEFAULT_CONFIG_TEXT, _IGNORED_CONFIG_SECTIONS
from entrypoints.cli.errors import _configuration_error, _project_resume_mismatch
from vibesys.api import ComputeBackend, Config, DomainName, load_config
from vibesys.api.request import (
    REPOSITORY_SLUG,
    default_skill_roots,
    generate_experiment_name,
    repository_name_from_experiment,
    resolve_skill_source_dirs,
)
from vs_github import GitHubCLI, GitHubCLIError

if TYPE_CHECKING:
    import argparse

    from vs_project import RunConfiguration


def _explicit_config_value(raw: object, path: tuple[str, ...]) -> tuple[bool, object]:
    current = raw
    for component in path:
        if not isinstance(current, dict) or component not in current:
            return False, None
        current = current[component]
    return True, current


def _restore_project_config(
    args: argparse.Namespace,
    config: Config,
    recorded: RunConfiguration,
) -> Config:
    """Restore persisted model settings, rejecting explicit config changes."""
    raw: object = {}
    if args.config is not None:
        with args.config.open("rb") as config_file:
            raw = tomllib.load(config_file)

    explicit_cli = getattr(args, "explicit_cli_dests", frozenset())
    specs = (
        (("model", "name"), recorded.model, frozenset(), "model"),
        (
            ("backend", "name"),
            recorded.compute_backend,
            frozenset({"backend"}),
            "compute_backend",
        ),
        (
            ("agent", "backend"),
            recorded.agent_backend,
            frozenset({"agent_backend", "stub_agent"}),
            "agent_backend",
        ),
        (("agent", "driver"), recorded.agent_driver, frozenset(), "agent_driver"),
        (
            ("agent", "cli_provider"),
            recorded.cli_provider,
            frozenset({"cli_provider", "stub_agent"}),
            "cli_provider",
        ),
        (("agent", "cli_timeout"), recorded.cli_timeout, frozenset(), "cli_timeout"),
        (
            ("thinking", "level"),
            recorded.default_reasoning_effort,
            frozenset(),
            "default_reasoning_effort",
        ),
        (("agent", "outer", "model"), recorded.outer_model, frozenset(), "outer_model"),
        (
            ("agent", "outer", "reasoning_effort"),
            recorded.outer_reasoning_effort,
            frozenset(),
            "outer_reasoning_effort",
        ),
        (("agent", "inner", "model"), recorded.inner_model, frozenset(), "inner_model"),
        (
            ("agent", "inner", "reasoning_effort"),
            recorded.inner_reasoning_effort,
            frozenset(),
            "inner_reasoning_effort",
        ),
    )
    changed: list[str] = []
    for path, expected, cli_overrides, field in specs:
        supplied, value = _explicit_config_value(raw, path)
        if supplied and not cli_overrides.intersection(explicit_cli) and value != expected:
            changed.append(field)
    if changed:
        _project_resume_mismatch(changed)

    outer = config.agent.outer.model_copy(
        update={
            "model": recorded.outer_model,
            "reasoning_effort": recorded.outer_reasoning_effort,
        }
    )
    inner = config.agent.inner.model_copy(
        update={
            "model": recorded.inner_model,
            "reasoning_effort": recorded.inner_reasoning_effort,
        }
    )
    agent = config.agent.model_copy(
        update={
            "backend": None if recorded.agent_backend == "stub" else recorded.agent_backend,
            "driver": recorded.agent_driver,
            "cli_provider": recorded.cli_provider,
            "cli_timeout": recorded.cli_timeout,
            "outer": outer,
            "inner": inner,
        }
    )
    return config.model_copy(
        update={
            "model": config.model.model_copy(update={"name": recorded.model or config.model.name}),
            "thinking": config.thinking.model_copy(
                update={"level": recorded.default_reasoning_effort, "budget": None}
            ),
            "backend": config.backend.model_copy(
                update={"name": ComputeBackend(recorded.compute_backend)}
            ),
            "agent": agent,
        }
    )


def load_config_and_skills(
    args: argparse.Namespace,
    *,
    domain: DomainName,
) -> tuple[Config, list[str] | None, ComputeBackend]:
    """Load config, resolve the backend, and select compatible skills."""
    config = _load_effective_config(args)

    repository = getattr(args, "repo", None)
    if getattr(args, "local", False) and repository is not None:
        _configuration_error(
            "--local cannot be combined with --repo",
            code="invalid_repository",
            stage="repository_setup",
        )
    if repository is not None:
        if "/" not in repository:
            owner = _resolve_repository_owner(config)
            repository = f"{owner}/{repository}"
        if not REPOSITORY_SLUG.fullmatch(repository):
            _configuration_error(
                f"--repo must be NAME with a configured or authenticated owner, or an "
                f"explicit GitHub OWNER/NAME pair, got {repository!r}",
                code="invalid_repository",
                stage="repository_setup",
            )
        args.repo = repository

    if getattr(args, "repo_visibility", None) is None:
        args.repo_visibility = config.repository.visibility

    backend: ComputeBackend = args.backend or config.backend.name

    if getattr(args, "no_skills", False):
        skills = None
    else:
        # --skills-dir overrides the presets; when omitted, the presets are the
        # base. --extra-skills always stacks on top (presets or the override).
        base = getattr(args, "skills_dir", None) or list(default_skill_roots())
        extra = getattr(args, "extra_skills", None) or []
        skills = resolve_skill_source_dirs([*base, *extra], backend=backend, domain=domain)
    return config, skills, backend


def _load_effective_config(args: argparse.Namespace) -> Config:
    """Load configuration and restore persisted settings for project resumes."""
    try:
        config = _load_config_or_stub_default(
            args.config,
            stub_agent=getattr(args, "stub_agent", False),
        )
    except (ValueError, FileNotFoundError) as e:
        _configuration_error(str(e), code="config_load_failed", stage="config_loading")

    recorded = getattr(args, "project_run_configuration", None)
    if recorded is not None:
        config = _restore_project_config(args, config, recorded)
    return config


def _resolve_repository_owner(config: Config) -> str:
    """Resolve the configured repository owner or the authenticated ``gh`` user."""
    if config.repository.owner is not None:
        return config.repository.owner
    try:
        return GitHubCLI().current_user()
    except GitHubCLIError as exc:
        _configuration_error(
            str(exc),
            code="repository_setup_failed",
            stage="repository_setup",
        )


def _load_config_or_stub_default(
    config_path: Path | None,
    *,
    stub_agent: bool,
) -> Config:
    """Load explicit or launch-directory config, then use safe built-in defaults."""
    if config_path is not None:
        return load_config(config_path, ignored_sections=_IGNORED_CONFIG_SECTIONS)
    selected_path = Path.cwd() / "agent.toml"
    if selected_path.is_file():
        return load_config(selected_path, ignored_sections=_IGNORED_CONFIG_SECTIONS)
    del stub_agent
    return Config.model_validate(tomllib.loads(_DEFAULT_CONFIG_TEXT))


def _prepare_experiment_repository(args: argparse.Namespace, config: Config) -> None:
    """Resolve fresh-run naming and remote selection before entering a loop."""
    if args.resume is not None:
        return

    if args.exp_name is None:
        args.exp_name = generate_experiment_name(args.input_bundle.root)

    # A direct project run stays local unless publication was requested
    # explicitly. Copied projects retain the convenient generated remote.
    if args.runs_dir is None and args.repo is None:
        args.local = True
        return

    if args.local:
        return

    if args.repo is None:
        owner = _resolve_repository_owner(config)
        args.repo = f"{owner}/{repository_name_from_experiment(args.exp_name)}"


def _prepare_stub_agent_smoke_defaults(argv: list[str]) -> list[str]:
    """Keep stub invocations on the same cwd-input path as real users."""
    return argv
