"""Agent config loading, skill selection, and resume-time config restoration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.cli.constants import _IGNORED_CONFIG_SECTIONS
from entrypoints.cli.errors import _cause_error, _configuration_error, _project_resume_mismatch
from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigInvalid,
    DomainName,
    GitHubUnavailable,
    load_config,
)
from vibesys.api.request import (
    REPOSITORY_SLUG,
    default_skill_roots,
    repository_name_from_experiment,
    resolve_skill_source_dirs,
)
from vs_github.api import GitHubCLI, GitHubCLIError
from vs_sandbox.api import backend_provided
from vs_sim.api import OsProcesses, SubprocessProbe

if TYPE_CHECKING:
    import argparse
    from types import SimpleNamespace

    from vibesys.api.request import InputBundle
    from vs_sim.api import CommandProbe


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
    recorded: SimpleNamespace,
) -> Config:
    """Restore persisted model settings, rejecting explicit config changes."""
    raw: object = {}
    if args.config is not None:
        with args.config.open("rb") as config_file:
            raw = tomllib.load(config_file)

    explicit_cli = getattr(args, "explicit_cli_dests", frozenset())
    specs = (
        (("model", "name"), recorded.model, frozenset({"model"}), "model"),
        (
            ("backend", "name"),
            recorded.compute_backend,
            frozenset({"backend"}),
            "compute_backend",
        ),
        (
            ("agent", "backend"),
            recorded.agent_backend,
            frozenset({"agent_backend"}),
            "agent_backend",
        ),
        (
            ("agent", "cli_provider"),
            recorded.cli_provider,
            frozenset({"cli_provider"}),
            "cli_provider",
        ),
        (("agent", "cli_timeout"), recorded.cli_timeout, frozenset(), "cli_timeout"),
        (
            ("thinking", "level"),
            recorded.default_reasoning_effort,
            frozenset(),
            "default_reasoning_effort",
        ),
        (("thinking", "budget"), recorded.thinking_budget, frozenset(), "thinking_budget"),
    )
    changed: list[str] = []
    for path, expected, cli_overrides, field in specs:
        supplied, value = _explicit_config_value(raw, path)
        if supplied and not cli_overrides.intersection(explicit_cli) and value != expected:
            changed.append(field)
    if changed:
        _project_resume_mismatch(changed)

    raw_roles = raw.get("agent", {}).get("roles", {}) if isinstance(raw, dict) else {}
    if isinstance(raw_roles, dict):
        unknown_roles = sorted(raw_roles.keys() - recorded.agent_roles.keys())
        changed.extend(f"agent.roles.{role_id}" for role_id in unknown_roles)
        for role_id, role in recorded.agent_roles.items():
            # A role that records no provider runs the run's.
            recorded_role = {**role, "provider": role["provider"] or recorded.cli_provider}
            for field in ("provider", "model", "reasoning_effort"):
                supplied, value = _explicit_config_value(raw, ("agent", "roles", role_id, field))
                if supplied and value != recorded_role[field]:
                    changed.append(f"agent.roles.{role_id}.{field}")
    if changed:
        _project_resume_mismatch(changed)

    agent = config.agent.__class__.model_validate(
        {
            **config.agent.model_dump(mode="python"),
            "backend": recorded.agent_backend,
            "cli_provider": recorded.cli_provider,
            "cli_timeout": recorded.cli_timeout,
            "roles": recorded.agent_roles,
        }
    )
    return config.model_copy(
        update={
            "model": config.model.model_copy(update={"name": recorded.model or config.model.name}),
            "thinking": config.thinking.model_copy(
                update={
                    "level": recorded.default_reasoning_effort,
                    "budget": recorded.thinking_budget,
                }
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
    probe: CommandProbe | None = None,
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
        # Configured owners are pure inputs. Discovering the current gh
        # user waits until the complete run request has been validated.
        if "/" not in repository and config.repository.owner is not None:
            repository = f"{config.repository.owner}/{repository}"
        candidate = repository if "/" in repository else f"owner/{repository}"
        if not REPOSITORY_SLUG.fullmatch(candidate):
            _configuration_error(
                f"--repo must be NAME with a configured or authenticated owner, or an "
                f"explicit GitHub OWNER/NAME pair, got {repository!r}",
                code="invalid_repository",
                stage="repository_setup",
            )
        args.repo = repository

    if getattr(args, "repo_visibility", None) is None:
        args.repo_visibility = config.repository.visibility

    backend = resolve_backend(args, config, probe)

    if getattr(args, "no_skills", False):
        skills = None
    else:
        # --skills-dir overrides the presets; when omitted, the presets are the
        # base. --extra-skills always stacks on top (presets or the override).
        base = getattr(args, "skills_dir", None) or list(default_skill_roots())
        extra = getattr(args, "extra_skills", None) or []
        skills = resolve_skill_source_dirs([*base, *extra], backend=backend, domain=domain)
    return config, skills, backend


def resolve_backend(
    args: argparse.Namespace, config: Config, probe: CommandProbe | None = None
) -> ComputeBackend:
    """The compute backend a run targets: the task's, not the user's.

    A resumed run keeps the backend it recorded. Otherwise a task that declares backends
    (``backends`` in its manifest) decides: its one backend, or the first it declares that
    this host provides (``probe``, a real one by default, is asked only when several are
    declared; none provided keeps the first, which preflight reports). ``--backend`` is an
    explicit override and must be one the task declares, else ``ConfigurationError``. A task
    that declares none (one written before backends were declared) falls back, explicitly, to
    ``--backend``, else ``[backend].name`` in ``agent.toml``, whose default is ``cuda``.
    """
    if getattr(args, "project_run_configuration", None) is not None:
        return config.backend.name
    bundle: InputBundle | None = getattr(args, "input_bundle", None)
    declared = () if bundle is None else bundle.declared_backends
    if args.backend is not None:
        if bundle is not None and declared and args.backend not in declared:
            _configuration_error(
                f"--backend {args.backend} is not a backend task {bundle.task_name!r} declares "
                f"({', '.join(declared)}); drop --backend, or declare it in the task's "
                "vibesys.input.toml",
                code="backend_not_declared",
                stage="compute_backend",
            )
        return args.backend
    if not declared:
        return config.backend.name
    if len(declared) == 1:
        return declared[0]
    probe = probe or SubprocessProbe()
    return next((each for each in declared if backend_provided(each, probe)), declared[0])


def _load_effective_config(args: argparse.Namespace) -> Config:
    """Load configuration and restore persisted settings for project resumes."""
    try:
        config = _load_config_or_default(args.config)
    except (ValueError, FileNotFoundError) as e:
        _cause_error(ConfigInvalid(problem=str(e)))

    model = getattr(args, "model", None)
    if model is not None:
        config = config.model_copy(
            update={"model": config.model.model_copy(update={"name": model})}
        )
    config = _with_run_overrides(config, args)
    recorded = getattr(args, "project_run_configuration", None)
    if recorded is not None:
        config = _restore_project_config(args, config, recorded)
    return config


def _with_run_overrides(config: Config, args: argparse.Namespace) -> Config:
    """``config`` with the run's time limit and role settings from the command line.

    The overrides are validated as if ``agent.toml`` held them, so an error names the key.
    """
    seconds = getattr(args, "max_run_seconds", None)
    role_providers: list[tuple[str, str]] = getattr(args, "role_provider", [])
    role_models: list[tuple[str, str]] = getattr(args, "role_model", [])
    role_efforts: list[tuple[str, str]] = getattr(args, "role_reasoning_effort", [])
    if seconds is None and not role_providers and not role_models and not role_efforts:
        return config
    document = config.model_dump(mode="json", exclude_unset=False)
    if seconds is not None:
        document["run"]["max_run_seconds"] = seconds
    roles = document["agent"]["roles"]
    for key, settings in (
        ("provider", role_providers),
        ("model", role_models),
        ("reasoning_effort", role_efforts),
    ):
        for role, value in settings:
            roles.setdefault(role, {})[key] = value
    try:
        return Config.model_validate(document)
    except ValueError as exc:
        _cause_error(ConfigInvalid(problem=str(exc)))


def _resolve_repository_owner(config: Config, github: GitHubCLI | None = None) -> str:
    """Resolve the configured repository owner or the authenticated ``gh`` user.

    ``github`` asks ``gh`` (the real one by default), only when no owner is configured.
    """
    if config.repository.owner is not None:
        return config.repository.owner
    try:
        return (github or GitHubCLI(OsProcesses())).current_user()
    except GitHubCLIError as exc:
        _cause_error(GitHubUnavailable(problem=str(exc)))


def _load_config_or_default(config_path: Path | None) -> Config:
    """Load explicit or launch-directory config, then use safe built-in defaults."""
    if config_path is not None:
        return load_config(config_path, ignored_sections=_IGNORED_CONFIG_SECTIONS)
    selected_path = Path.cwd() / "agent.toml"
    if selected_path.is_file():
        return load_config(selected_path, ignored_sections=_IGNORED_CONFIG_SECTIONS)
    return Config()


def _publishes_to_a_remote(args: argparse.Namespace) -> bool:
    """Whether the run creates or pushes to a GitHub repository (``--repo``, ``--publish``)."""
    if args.resume is not None:
        return False
    if args.runs_dir is None and args.repo is None and not args.publish:
        return False
    return not args.local


def require_repository_owner(
    args: argparse.Namespace, config: Config, github: GitHubCLI | None = None
) -> None:
    """Raise ``ConfigurationError`` (``GitHubUnavailable``) when the run needs a GitHub account it lacks.

    It asks what ``_prepare_experiment_repository`` asks, ahead of the run: a repository named
    without an owner, or a new one to create, needs the configured owner or the signed-in
    ``gh`` account. ``github`` is the client to ask (the real one by default).
    """
    named_without_owner = args.repo is not None and "/" not in args.repo
    if named_without_owner or (_publishes_to_a_remote(args) and args.repo is None):
        _resolve_repository_owner(config, github)


def _prepare_experiment_repository(args: argparse.Namespace, config: Config) -> None:
    """Resolve remote selection after pure run-request validation."""
    if args.repo is not None and "/" not in args.repo:
        args.repo = f"{_resolve_repository_owner(config)}/{args.repo}"
    if args.resume is not None:
        return

    # A direct project run stays local unless publication was requested
    # explicitly. Copied projects retain the convenient generated remote.
    if args.runs_dir is None and args.repo is None and not args.publish:
        args.local = True
        return

    if args.local:
        return

    if args.repo is None:
        owner = _resolve_repository_owner(config)
        args.repo = f"{owner}/{repository_name_from_experiment(args.exp_name)}"
