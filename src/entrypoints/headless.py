"""Headless Python entry point for VibeSys runs.

The loop is picked by ``--outer-loop {agent, plain, evolve}``:

  "agent"  — an LLM Orchestrator decides per-round what to build next.
             Its issue board lives in the workspace as roadmap.md +
             progress.md, owned by the orchestrator.
  "plain"  — deterministic outer loop. Its issue board is a structured
             :class:`IssueBoard` (issues.json) that perf_eval files into
             and the implementer drains one issue at a time.
  "evolve" — population-based evolutionary search.

The TypeScript launcher owns interactive orchestration. This module owns
Python-side argument parsing, validation, and loop dispatch.
"""

from __future__ import annotations

import argparse
import math
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from vibesys import boot_trace
from vibesys.agents.provider_policy import SHIPPED_PROVIDERS
from vibesys.config import Config, load_config
from vibesys.constants import (
    KNOWN_COMPUTE_BACKENDS,
    PROJECT_ROOT,
    ComputeBackend,
)
from vibesys.domains.base import DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.input_manifest import InputBundle, load_input_bundle, load_project_task
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.loops.roles import expected_agent_roles
from vibesys.profilers import CLI_PROFILER_CHOICES, ProfilerKind, coerce_profiler_kind
from vibesys.render.headless import HeadlessRenderer
from vibesys.repository import (
    REPOSITORY_SLUG,
    RepositoryVisibility,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.resource_paths import default_skill_roots
from vibesys.run.events import CoreEventType, EventStatus, RunStartedData
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.run.git_tracker import GitTracker
from vibesys.run.integration import LocalRunIntegration, RunIntegration
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.sandbox.task_image import build_task_image
from vibesys.skills import resolve_skill_source_dirs
from vs_github import GitHubCLI, GitHubCLIError
from vs_project import (
    AgentRunConfiguration,
    PlainRunConfiguration,
    Project,
    ProjectLayoutError,
    ProjectStateError,
    RunConfiguration,
    RunSchemaMigrationRequiredError,
)

if TYPE_CHECKING:
    from vibesys.loops.evolve.search_policy import OpenEvolveSearchConfig

__all__ = ["PROJECT_ROOT"]

_OUTER_LOOPS = ("agent", "plain", "evolve")
_MODALITIES = (
    "text_generation",
    "image_generation",
    "video_generation",
    "text_to_speech",
    "speech_to_text",
    "realtime_audio",
    "kv_store",
    "dataflow_opt",
)

_DEFAULT_CONFIG_TEXT = '[model]\nname = "gpt-5.4"\n'
_IGNORED_CONFIG_SECTIONS = frozenset({"tui"})


class _RunArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports structured configuration errors."""

    def error(self, message: str) -> NoReturn:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="invalid_arguments",
                stage="argument_parsing",
                message=message,
                usage=self.format_usage().strip(),
            )
        )


@dataclass(frozen=True)
class CliInvocation:  # tracked: #288
    loop_kind: str
    args: argparse.Namespace


_COMMON_RESUME_CLI_FIELDS: dict[str, str] = {
    "agent_backend": "agent_backend",
    "cli_provider": "cli_provider",
    "backend": "compute_backend",
    "profiler": "profiler",
    "modality": "modality",
}

# CLI destination -> ``RunEnvironmentRecord`` field for the runtime-environment
# options. The environment kind itself comes from the ``--docker`` / ``--modal``
# store-true flags and is handled separately.
_RUN_ENVIRONMENT_OPTION_CLI_FIELDS: dict[str, str] = {
    "docker_image": "image",
    "modal_gpu": "gpu",
    "modal_model_volume": "model_volume",
    "modal_app": "app",
}

_AGENT_RESUME_CLI_FIELDS: dict[str, str] = {
    "inner_loop": "inner_loop",
    "interface": "interface",
    "max_retries_per_round": "max_retries_per_round",
    "judge_every": "judge_every",
    "official_eval_every": "official_eval_every",
    "memory_layout": "memory_layout",
}

_PLAIN_RESUME_CLI_FIELDS: dict[str, str] = {
    "max_attempts_per_issue": "max_attempts_per_issue",
    "max_issues_per_perf_eval": "max_issues_per_perf_eval",
}

_EVOLVE_RESUME_CLI_FIELDS: dict[str, str] = {
    "children_per_generation": "children_per_generation",
    "k_top_inspirations": "k_top_inspirations",
    "k_random_inspirations": "k_random_inspirations",
    "selection_temperature": "selection_temperature",
    "seed": "seed",
    "search_policy": "search_policy",
    "openevolve_population_size": "openevolve_population_size",
    "openevolve_archive_size": "openevolve_archive_size",
    "openevolve_num_islands": "openevolve_num_islands",
    "openevolve_migration_interval": "openevolve_migration_interval",
    "openevolve_migration_rate": "openevolve_migration_rate",
    "frontier_bias": "frontier_bias",
    "bootstrap_max_attempts": "bootstrap_max_attempts",
    "keep_deployments": "keep_deployments",
    "max_parallelism": "max_parallelism",
}


def _configuration_error(
    message: str,
    *,
    code: str = "invalid_configuration",
    stage: str = "semantic_validation",
    exit_code: int = 2,
) -> NoReturn:
    raise ConfigurationError(
        ConfigurationDiagnostic(
            code=code,
            stage=stage,
            message=message,
            exit_code=exit_code,
        )
    )


def _parse_profiler_kind(value: str) -> ProfilerKind:
    try:
        return coerce_profiler_kind(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_runs_dir(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")  # noqa: TRY003  # tracked: #288
    return Path(value)


# ---------------------------------------------------------------------------
# Loop selection from argv
# ---------------------------------------------------------------------------


def _extract_flag(argv: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull ``--flag <value>`` (or ``--flag=<value>``) out of argv.

    Returns ``(value_or_none, remaining_argv)``. The flag is consumed; all
    other tokens are preserved in order.
    """
    out: list[str] = []
    value: str | None = None
    eq_form = f"{flag}="
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == flag:
            if i + 1 >= len(argv):
                _fail(f"{flag} requires a value")
            value = argv[i + 1]
            i += 2
            continue
        if tok.startswith(eq_form):
            value = tok[len(eq_form) :]
            i += 1
            continue
        out.append(tok)
        i += 1
    return value, out


def _extract_loop_selection(argv: list[str]) -> tuple[str, list[str]]:
    """Pull ``--outer-loop`` from argv and resolve to a loop kind.

    Returns ``(loop_kind, remaining_argv)`` where ``loop_kind`` is one of
    ``"agent"`` / ``"plain"`` / ``"evolve"``. Defaults to ``"agent"``
    when the flag is absent.
    """
    outer_loop, rest = _extract_flag(argv, "--outer-loop")

    if outer_loop is None:
        outer_loop = "agent"
    if outer_loop not in _OUTER_LOOPS:
        _fail(f"unknown --outer-loop {outer_loop!r}; choose from {', '.join(_OUTER_LOOPS)}")

    return outer_loop, rest


def _fail(msg: str) -> None:
    raise ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message=msg,
            usage=(
                f"Usage: vibesys --outer-loop {{{'|'.join(_OUTER_LOOPS)}}} [loop-specific args...]"
            ),
        )
    )


# ---------------------------------------------------------------------------
# Common parser scaffolding
# ---------------------------------------------------------------------------


#: Standalone-input flag dests that together synthesize a bundle when ``--input``
#: is omitted. Kept in one place so conflict/requirement checks stay in sync.
#: Every flag is spelled ``--input-<name>``; argparse maps it to ``input_<name>``.
_STANDALONE_INPUT_DESTS = (
    "input_objective",
    "input_objective_file",
    "input_domain",
    "input_accuracy_command",
    "input_benchmark_command",
    "input_accuracy_timeout",
    "input_benchmark_timeout",
    "input_benchmark_metric",
    "input_benchmark_result_arg",
    "input_reference",
    "input_evaluator_dir",
    "input_evaluator_source",
)


def _add_standalone_input_args(parser: argparse.ArgumentParser) -> None:
    """Add flags that synthesize an input bundle without a prebuilt ``--input``.

    These let external users (e.g. a ``pip install``ed VibeSys with no
    repository ``examples/`` on disk) pass the objective, domain, and evaluator
    commands directly. When any are set and ``--input`` is omitted, the flags
    are materialized into a bundle before the normal input-loading path runs.

    All flags share the ``--input-`` prefix so they read as the pieces of
    ``--input`` provided separately, and so none abbreviate to a retired flag
    (e.g. ``--ref``, ``--domain``) that the CLI still rejects.
    """
    group = parser.add_argument_group(
        "standalone input",
        "Provide an input bundle's contents directly instead of --input. "
        "Requires --input-objective/--input-objective-file, --input-domain, "
        "--input-accuracy-command, and --input-benchmark-command; the rest are optional.",
    )
    group.add_argument(
        "--input-objective",
        default=None,
        metavar="TEXT",
        help="Objective text (becomes OBJECTIVE.md). Mutually exclusive with --input-objective-file.",
    )
    group.add_argument(
        "--input-objective-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to a file whose contents become OBJECTIVE.md.",
    )
    group.add_argument(
        "--input-domain",
        type=DomainName,
        choices=list(DomainName),
        default=None,
        help="Target domain for the synthesized bundle's [agent].domain.",
    )
    group.add_argument(
        "--input-accuracy-command",
        default=None,
        metavar="CMD",
        help="Accuracy evaluator command, shell-quoted (e.g. 'python checker.py').",
    )
    group.add_argument(
        "--input-benchmark-command",
        default=None,
        metavar="CMD",
        help="Benchmark command, shell-quoted (e.g. 'python benchmark.py').",
    )
    group.add_argument(
        "--input-accuracy-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Optional timeout for the accuracy command.",
    )
    group.add_argument(
        "--input-benchmark-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Optional timeout for the benchmark command.",
    )
    group.add_argument(
        "--input-benchmark-metric",
        default=None,
        metavar="NAME",
        help=(
            "JSON field the benchmark emits as its scalar result "
            "(with --input-benchmark-result-arg)."
        ),
    )
    group.add_argument(
        "--input-benchmark-result-arg",
        default=None,
        metavar="OPT",
        help=(
            "Option-style argv element the benchmark accepts for its JSON result path "
            "(e.g. --result-json); pairs with --input-benchmark-metric."
        ),
    )
    group.add_argument(
        "--input-reference",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory copied into the bundle as reference/.",
    )
    group.add_argument(
        "--input-evaluator-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory whose contents are copied into the bundle root (evaluator scripts, etc.).",
    )
    group.add_argument(
        "--input-evaluator-source",
        type=Path,
        default=None,
        metavar="DIR",
        help="Trusted evaluator source directory (manifest [evaluator].source).",
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add CLI arguments shared across every outer-loop parser."""
    parser.add_argument(
        "--project",
        "--input",
        dest="input",
        type=Path,
        default=None,
        help=(
            "Candidate repository containing .vibesys/tasks. --input remains as a "
            "compatibility alias. Defaults to the launch working directory."
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        metavar="NAME",
        help="Task under .vibesys/tasks. May be omitted when the project defines one task.",
    )
    parser.add_argument(
        "--runs-dir",
        type=_parse_runs_dir,
        default=None,
        metavar="PATH",
        help=(
            "Materialize each run in an experiment collection under PATH. When omitted, "
            "VibeSys optimizes the input project in place and stores run state alongside it."
        ),
    )
    _add_standalone_input_args(parser)
    parser.add_argument(
        "--exp-name",
        required=False,
        default=None,
        help="Experiment name; generated from the input bundle when omitted.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to agent TOML config file. When omitted, VibeSys reads agent.toml "
            "from the launch directory if present, otherwise uses built-in CLI defaults."
        ),
    )
    parser.add_argument(
        "--profiler",
        type=_parse_profiler_kind,
        choices=CLI_PROFILER_CHOICES,
        default=ProfilerKind.AUTO,
        help=(
            "Which profiler to use between rounds. "
            "'none' to disable standalone profiling, "
            "'nsys' for NVIDIA Nsight Systems (needs /proc/driver/nvidia), "
            "'torch' for torch.profiler (works under the Modal run environment), "
            "'neuron' for AWS neuron-explorer (Trainium/NeuronCores), "
            "'otel' for OpenTelemetry service/span/datastore latency on "
            "microservice benchmarks (opt-in; needs an instrumented input bundle), "
            "'macos_cpu' for Instruments Time Profiler with a sample fallback, "
            "'linux_cpu' for Linux perf on native CPU workloads, "
            "'auto' picks a domain/backend/environment-appropriate profiler. "
            "Default: auto."
        ),
    )
    parser.add_argument(
        "--skills-dir",
        default=None,
        action="append",
        type=Path,
        metavar="PATH",
        help=(
            "Skill source that REPLACES the built-in preset roots (can be "
            "repeated). Each entry is a skill directory (containing a top-level "
            "`SKILL.md`), a parent directory of many skills, or a single "
            "`SKILL.md` file. When omitted, the preset `resources/skills/` is "
            "used. To keep the presets and add your own, use --extra-skills."
        ),
    )
    parser.add_argument(
        "--extra-skills",
        default=None,
        action="append",
        type=Path,
        metavar="PATH",
        help=(
            "Additional skill source stacked ON TOP of the preset roots (or on "
            "top of --skills-dir when that is given). Repeat for multiple. Each "
            "entry is a skill directory, a parent directory of many skills, or a "
            "single `SKILL.md` file. Skills with VibeSys routing metadata still "
            "load only for matching domains and backends; a same-named skill "
            "from here overrides a preset one."
        ),
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help=(
            "Disable skills entirely: no skill directories are copied into "
            "the workspace and no per-CLI skill-discovery paths are populated. "
            "Used for ablations measuring the skill library's contribution. "
            "Overrides --skills-dir and --extra-skills."
        ),
    )
    parser.add_argument(
        "--run-environment",
        choices=("local", "docker", "modal", "skypilot"),
        default=None,
        help=(
            "Select where trusted work runs. SkyPilot and Modal keep the agent "
            "in a local CPU-only Docker editor."
        ),
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help=(
            "Run agent operations inside a Docker container. On --resume the "
            "recorded runtime environment is restored when no runtime-environment "
            "flag is given, and a flag that contradicts the recording is rejected."
        ),
    )
    parser.add_argument(
        "--docker-image",
        type=str,
        default=None,
        help=(
            "Docker image to use (with --docker or --modal).  Defaults to the "
            "image the selected --backend prefers (cuda → nvcr.io/nvidia/pytorch:25.04-py3)."
        ),
    )
    parser.add_argument(
        "--modal",
        action="store_true",
        help=(
            "Use Modal for remote GPU dispatch. The agent (codex) still runs "
            "locally inside a Docker container for editing; GPU-bound code "
            "the implementer writes (decorated with `@app.cls` / `@app.function`) "
            "is dispatched via `modal run`. Mutually exclusive with --docker. "
            "On --resume the recorded runtime environment is restored when no "
            "runtime-environment flag is given, and a flag that contradicts the "
            "recording is rejected."
        ),
    )
    parser.add_argument(
        "--modal-gpu",
        type=str,
        default="H100!",
        help=(
            "Default Modal GPU spec for `@app.function` / `@app.cls` decorators "
            "(e.g. H100!, A100-80GB, L40S). The default `H100!` disables "
            "Modal's automatic H100-to-H200 benchmark upgrade."
        ),
    )
    parser.add_argument(
        "--modal-model-volume",
        type=str,
        default=None,
        help=(
            "Name of a pre-existing Modal Volume holding model weights "
            "(referenced by the implementer's `modal.Volume.from_name(...)` calls)."
        ),
    )
    parser.add_argument(
        "--modal-app",
        type=str,
        default="vibesys",
        help="Default Modal App name (suggested to the implementer). Default: vibesys.",
    )
    parser.add_argument(
        "--skypilot",
        action="store_true",
        help=(
            "Use a CPU-only Docker editor and dispatch trusted evaluator commands "
            "through a host-owned SkyPilot allocation."
        ),
    )
    parser.add_argument(
        "--cluster-profile",
        default=None,
        help=(
            "Operator-owned SkyPilot cluster profile name. Select it on each "
            "fresh or resumed SkyPilot launch; it is not an immutable run input."
        ),
    )
    parser.add_argument(
        "--cluster-profiles-file",
        type=Path,
        default=None,
        help="Cluster profile TOML file (default: ~/.config/vibesys/clusters.toml).",
    )
    parser.add_argument(
        "--skypilot-executable",
        default="sky",
        help="SkyPilot CLI executable. Default: sky.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Pause for Enter at each step in loop mode.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="[OWNER/]NAME",
        help=(
            "Override the generated GitHub repository name for this experiment. "
            "A configured [repository].owner or authenticated `gh` account supplies "
            "an omitted owner."
        ),
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Keep this experiment local under --runs-dir; do not create or sync GitHub.",
    )
    parser.add_argument(
        "--repo-visibility",
        type=RepositoryVisibility,
        choices=list(RepositoryVisibility),
        default=None,
        help=(
            "Visibility for the experiment repository. Defaults to "
            "[repository].visibility in agent.toml."
        ),
    )
    parser.add_argument(
        "--agent-backend",
        choices=["deepagents", "cli"],
        default=None,
        help=(
            "Which agent backend implementation to use. Overrides "
            "[agent].backend in agent.toml. Defaults to 'cli'."
        ),
    )
    parser.add_argument(
        "--cli-provider",
        choices=list(SHIPPED_PROVIDERS),
        default=None,
        help=(
            "Which CLI tool to drive when --agent-backend=cli. Overrides "
            "[agent].cli_provider in agent.toml."
        ),
    )
    parser.add_argument(
        "--backend",
        type=ComputeBackend,
        choices=list(ComputeBackend),
        default=None,
        help=(
            "Compute backend to target. Overrides [backend].name in "
            f"agent.toml. Defaults to 'cuda'. Supported: {', '.join(KNOWN_COMPUTE_BACKENDS)}."
        ),
    )


def _project_resume_mismatch(fields: list[str]) -> NoReturn:
    _configuration_error(
        "Resuming a run cannot change its recorded configuration fields: "
        + ", ".join(sorted(fields)),
        code="project_resume_configuration_mismatch",
        stage="resume_resolution",
    )


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


def run_environment_spec_from_args(  # tracked: #288
    args: argparse.Namespace,
    *,
    build_task_docker_image: bool = False,
) -> RunEnvironmentSpec:
    bundle = getattr(args, "input_bundle", None)
    selected = getattr(args, "run_environment", None)
    explicit = getattr(args, "explicit_cli_dests", frozenset())
    if "run_environment" in explicit and {"docker", "modal", "skypilot"} & explicit:
        raise ValueError(  # noqa: TRY003
            "--run-environment cannot be combined with --docker, --modal, or --skypilot"
        )
    compatibility_selections = (
        args.docker,
        args.modal,
        getattr(args, "skypilot", False),
    )
    if sum(compatibility_selections) > 1:
        raise ValueError("--docker, --modal, and --skypilot are mutually exclusive")  # noqa: TRY003

    dockerfile_path = bundle.dockerfile_path if bundle is not None else None
    resuming = getattr(args, "resume", None) is not None
    requested_environment = (
        selected
        or ("skypilot" if getattr(args, "skypilot", False) else None)
        or ("modal" if args.modal else None)
        or ("docker" if args.docker else None)
        or "local"
    )
    if dockerfile_path is not None and not resuming:
        conflicts: list[str] = []
        if requested_environment == "local" and "run_environment" in explicit:
            conflicts.append("--run-environment local")
        elif requested_environment == "modal":
            conflicts.append("--run-environment modal" if selected else "--modal")
        elif requested_environment == "skypilot":
            conflicts.append("--run-environment skypilot" if selected else "--skypilot")
        if "docker_image" in explicit:
            conflicts.append("--docker-image")
        if conflicts:
            joined = ", ".join(conflicts)
            raise ValueError(  # noqa: TRY003
                f"task Dockerfile {dockerfile_path} cannot be combined with {joined}"
            )
        requested_environment = "docker"

    task_image = None
    if (
        dockerfile_path is not None
        and requested_environment == "docker"
        and build_task_docker_image
    ):
        task_image = build_task_image(dockerfile_path)

    return make_run_environment_spec(
        use_docker=requested_environment == "docker",
        docker_image=task_image or args.docker_image,
        use_modal=requested_environment == "modal",
        modal_gpu=args.modal_gpu,
        modal_model_volume=args.modal_model_volume,
        modal_app=args.modal_app,
        modal_entrypoint=bundle.modal_entrypoint if bundle is not None else None,
        use_skypilot=requested_environment == "skypilot",
        cluster_profile=getattr(args, "cluster_profile", None),
        cluster_profiles_file=getattr(args, "cluster_profiles_file", None),
        skypilot_executable=getattr(args, "skypilot_executable", "sky"),
        resources=bundle.manifest.resources if bundle is not None else None,
    )


def _validate_run_environment_profiler(args: argparse.Namespace) -> None:
    """Validate profiler compatibility through the selected adapter contract."""
    spec = run_environment_spec_from_args(args)
    supported = build_run_environment(spec).supported_profiler_kinds
    if supported is None or args.profiler in supported:
        return
    allowed = ", ".join(sorted(kind.value for kind in supported))
    _configuration_error(
        f"Error: run environment {spec.name!r} does not support "
        f"--profiler={args.profiler.value}; allowed: {allowed}."
    )


def _normalize_runs_dir(args: argparse.Namespace) -> None:
    raw = getattr(args, "runs_dir", None)
    if raw is None:
        if _standalone_input_dests_set(args):
            _configuration_error(
                "Standalone --input-* flags require --runs-dir because they synthesize "
                "a new input bundle. Use a self-contained project directory for a direct run.",
                code="missing_runs_dir",
                stage="argument_parsing",
            )
        args.runs_dir = None
        return
    runs_dir = raw.expanduser().resolve()
    prefix = Path(sys.prefix).resolve()
    if runs_dir.is_relative_to(prefix):
        _configuration_error(
            f"--runs-dir cannot be inside the Python installation prefix {prefix}: {runs_dir}",
            code="invalid_runs_dir",
            stage="argument_parsing",
        )
    if runs_dir.exists() and not runs_dir.is_dir():
        _configuration_error(
            f"--runs-dir is not a directory: {runs_dir}",
            code="invalid_runs_dir",
            stage="argument_parsing",
        )
    args.runs_dir = runs_dir


def _resolve_project_root(project_arg: str, runs_dir: Path) -> Path:
    """Resolve one canonical project from a local collection or remote URL."""
    if project_arg != "latest":
        explicit = Path(project_arg).expanduser()
        if explicit.is_dir():
            project_root = explicit.resolve()
            if Project.is_state_initialized(project_root):
                return project_root
            _configuration_error(
                f"Directory is not a VibeSys project: {project_root}",
                code="resume_not_found",
                stage="resume_resolution",
            )

        collection_path = runs_dir / project_arg
        if collection_path.is_dir():
            project_root = collection_path.resolve()
            if Project.is_state_initialized(project_root):
                return project_root
            _configuration_error(
                f"Directory is not a VibeSys project: {project_root}",
                code="resume_not_found",
                stage="resume_resolution",
            )

        if _is_remote_project(project_arg):
            return _clone_project(project_arg, runs_dir)
        _configuration_error(
            f"Project directory does not exist: {collection_path}",
            code="resume_not_found",
            stage="resume_resolution",
        )

    if not runs_dir.is_dir():
        _configuration_error(
            f"Runs directory does not exist: {runs_dir}",
            code="resume_not_found",
            stage="resume_resolution",
        )
    projects = Project.find_state_projects(runs_dir)
    if not projects:
        _configuration_error(
            f"No VibeSys projects found in {runs_dir}.",
            code="resume_not_found",
            stage="resume_resolution",
        )
    return projects[-1].resolve()


def _is_remote_project(value: str) -> bool:
    return bool(
        REPOSITORY_SLUG.fullmatch(value)
        or value.startswith(("file://", "https://", "ssh://", "git@"))
    )


@dataclass(frozen=True)
class _RemoteRunBranch:
    created_at: float
    run_id: str
    remote_branch: str
    branch: str


def _clone_project(remote: str, runs_dir: Path) -> Path:
    """Clone a remote project into *runs_dir* and return its root."""
    destination = runs_dir / _remote_repository_name(remote)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return _reuse_cloned_project(remote, destination)
    if REPOSITORY_SLUG.fullmatch(remote):
        try:
            GitHubCLI().clone_repository(remote, destination)
        except GitHubCLIError as exc:
            _configuration_error(
                f"Cannot clone project repository {remote!r}: {exc}",
                code="resume_clone_failed",
                stage="resume_resolution",
            )
        return _select_cloned_run_branch(destination.resolve())

    try:
        result = _resume_git(runs_dir, "clone", remote, str(destination))
    except FileNotFoundError as exc:
        _configuration_error(
            f"Cannot clone {remote!r}: required command 'git' is not installed ({exc})",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    _require_resume_git(result, f"Cannot clone project repository {remote!r}")
    return _select_cloned_run_branch(destination.resolve())


def _remote_repository_name(remote: str) -> str:
    repository_name = remote.rstrip("/").rsplit("/", 1)[-1]
    if ":" in repository_name:
        repository_name = repository_name.rsplit(":", 1)[-1]
    repository_name = repository_name.removesuffix(".git")
    if repository_name in {"", ".", ".."}:
        _configuration_error(
            f"Cannot determine a safe local directory name from --resume {remote!r}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    return repository_name


def _reuse_cloned_project(remote: str, destination: Path) -> Path:
    if not destination.is_dir():
        _configuration_error(
            f"Cannot clone project: destination is not a directory: {destination}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    existing_origin = _resume_git(destination, "remote", "get-url", "origin")
    expected_origin = remote.removesuffix(".git").rstrip("/")
    actual_origin = existing_origin.stdout.strip().removesuffix(".git").rstrip("/")
    origin_matches = existing_origin.returncode == 0 and (
        ExperimentRepository(destination, lambda _message: None).origin_matches(remote)
        if REPOSITORY_SLUG.fullmatch(remote)
        else actual_origin == expected_origin
    )
    if not origin_matches:
        _configuration_error(
            f"Cannot clone {remote!r}: destination already exists with a different origin: "
            f"{destination}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    fetched = _resume_git(destination, "fetch", "--prune", "origin")
    _require_resume_git(fetched, f"Cannot update cloned project {destination}")
    return _select_cloned_run_branch(destination.resolve())


def _select_cloned_run_branch(project_root: Path) -> Path:
    """Select the newest valid portable run after cloning its repository."""
    listed = _resume_git(
        project_root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/remotes/origin/vibesys-runs/",
        "refs/remotes/origin/vibesys/",
    )
    _require_resume_git(listed, f"Cannot inspect VibeSys run branches in {project_root}")
    candidates = _remote_run_candidates(project_root, listed.stdout.splitlines())
    if not candidates:
        _configuration_error(
            f"No valid VibeSys run branches were found in cloned project {project_root}.",
            code="resume_not_found",
            stage="resume_resolution",
        )

    selected = max(candidates, key=lambda candidate: (candidate.created_at, candidate.run_id))
    _checkout_remote_run_branch(project_root, selected)
    Project.open(project_root).state.set_current_run(selected.run_id)
    return project_root


def _remote_run_candidates(
    project_root: Path,
    remote_branches: list[str],
) -> list[_RemoteRunBranch]:
    candidates: list[_RemoteRunBranch] = []
    for remote_branch in remote_branches:
        identity = _remote_run_identity(remote_branch)
        if identity is None:
            continue
        branch, run_id = identity
        switched = _resume_git(project_root, "switch", "--detach", "--quiet", remote_branch)
        if switched.returncode != 0 or not Project.is_state_initialized(project_root):
            continue
        try:
            manifest = Project.open(project_root).state.load_run(run_id)
        except ProjectStateError:
            continue
        if manifest.branch == branch:
            candidates.append(
                _RemoteRunBranch(
                    created_at=manifest.created_at.timestamp(),
                    run_id=manifest.run_id,
                    remote_branch=remote_branch,
                    branch=branch,
                )
            )
    return candidates


def _remote_run_identity(remote_branch: str) -> tuple[str, str] | None:
    branch = remote_branch.removeprefix("origin/")
    for prefix in ("vibesys-runs/", "vibesys/"):
        if branch.startswith(prefix) and (run_id := branch.removeprefix(prefix)):
            return branch, run_id
    return None


def _checkout_remote_run_branch(project_root: Path, selected: _RemoteRunBranch) -> None:
    local_exists = _resume_git(
        project_root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{selected.branch}",
    )
    switch_arguments = (
        ("switch", "--quiet", selected.branch)
        if local_exists.returncode == 0
        else (
            "switch",
            "--quiet",
            "--track",
            "-c",
            selected.branch,
            selected.remote_branch,
        )
    )
    switched = _resume_git(project_root, *switch_arguments)
    _require_resume_git(switched, f"Cannot select VibeSys run branch {selected.branch!r}")
    if local_exists.returncode == 0:
        advanced = _resume_git(
            project_root,
            "merge",
            "--quiet",
            "--ff-only",
            selected.remote_branch,
        )
        _require_resume_git(
            advanced,
            f"Cannot fast-forward VibeSys run branch {selected.branch!r}",
        )
    upstream = _resume_git(project_root, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream.returncode == 0 and upstream.stdout.strip() == selected.remote_branch:
        return
    tracked = _resume_git(
        project_root,
        "branch",
        "--set-upstream-to",
        selected.remote_branch,
        selected.branch,
    )
    _require_resume_git(tracked, f"Cannot track VibeSys run branch {selected.remote_branch!r}")


def _resume_git(project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a non-shell Git command during remote resume resolution."""
    return subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
        ["git", *arguments],  # noqa: S607  # tracked: #288
        cwd=project_root,
        capture_output=True,
        text=True,
    )


def _require_resume_git(
    result: subprocess.CompletedProcess[str],
    message: str,
) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    _configuration_error(
        f"{message}: {detail}",
        code="resume_clone_failed",
        stage="resume_resolution",
    )


def _restore_resume_budget(
    args: argparse.Namespace,
    *,
    destination: str,
    recorded_value: int,
    explicit: frozenset[str],
) -> None:
    if destination not in explicit:
        setattr(args, destination, recorded_value)
        return
    requested = getattr(args, destination)
    if requested < recorded_value:
        option = "--" + destination.replace("_", "-")
        _configuration_error(
            f"{option} is the run's total limit and cannot decrease when resuming "
            f"(recorded {recorded_value}, requested {requested})",
            code="project_resume_configuration_mismatch",
            stage="resume_resolution",
        )


def _restore_resume_agent_backend(
    args: argparse.Namespace,
    recorded: AgentRunConfiguration,
    explicit: frozenset[str],
) -> bool:
    """Restore the backend and return whether an explicit value mismatched."""
    if not {"stub_agent", "agent_backend"}.intersection(explicit):
        args.stub_agent = recorded.agent_backend == "stub"
        args.agent_backend = None if args.stub_agent else recorded.agent_backend
        return False
    requested = "stub" if args.stub_agent else args.agent_backend
    return requested != recorded.agent_backend


def _restore_resume_constraints(
    args: argparse.Namespace,
    recorded: AgentRunConfiguration,
    explicit: frozenset[str],
) -> bool:
    """Restore constraints and return whether an explicit value mismatched."""
    if "constraint" not in explicit:
        args.constraint = list(recorded.operator_constraints)
        return False
    requested = tuple(constraint.strip() for constraint in args.constraint if constraint.strip())
    return requested != recorded.operator_constraints


def _restore_resume_run_environment(  # noqa: C901
    args: argparse.Namespace,
    recorded: RunConfiguration,
    explicit: frozenset[str],
) -> list[str]:
    """Restore the recorded runtime environment and reject contradictions.

    ``--docker`` and ``--modal`` are store-true flags, so an omitted flag is
    indistinguishable from ``--flag false``. The recorded environment therefore
    wins whenever the resume invocation says nothing about it, and only an
    explicitly passed flag that contradicts the recording is an error.
    """
    record = recorded.run_environment
    changed: list[str] = []
    if not hasattr(args, "docker") or not hasattr(args, "modal"):
        return changed
    skypilot = bool(getattr(args, "skypilot", False))
    generic = getattr(args, "run_environment", None)
    if {"docker", "modal", "skypilot", "run_environment"} & explicit:
        requested = (
            generic
            if "run_environment" in explicit
            else "skypilot"
            if skypilot
            else "modal"
            if args.modal
            else "docker"
            if args.docker
            else "local"
        )
        if requested != record.name:
            changed.append("run_environment")
    else:
        args.docker = record.name == "docker"
        args.modal = record.name == "modal"
        if hasattr(args, "skypilot"):
            args.skypilot = record.name == "skypilot"
        if hasattr(args, "run_environment"):
            args.run_environment = record.name

    for destination, field in _RUN_ENVIRONMENT_OPTION_CLI_FIELDS.items():
        if not hasattr(args, destination):
            continue
        expected = getattr(record, field)
        if destination in explicit:
            if getattr(args, destination) != expected:
                changed.append(f"run_environment.{field}")
        elif expected is not None:
            setattr(args, destination, expected)
    return changed


def _normalized_resume_cli_value(destination: str, value: object) -> object:
    if destination == "backend" and value is not None:
        assert isinstance(value, ComputeBackend)  # noqa: S101  # argparse contract
        return value.value
    if destination == "profiler":
        assert isinstance(value, ProfilerKind)  # noqa: S101  # argparse contract
        return ProfilerKind.NONE.value if value is ProfilerKind.AUTO else value.value
    return value


def _set_resume_cli_value(
    args: argparse.Namespace,
    destination: str,
    value: object,
) -> None:
    if destination == "backend":
        try:
            value = ComputeBackend(value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown compute backend {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    elif destination == "profiler":
        if value is not None and not isinstance(value, str):
            _configuration_error(
                f"Run metadata records unknown profiler {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
        try:
            value = coerce_profiler_kind(value or ProfilerKind.AUTO.value)
        except ValueError:
            _configuration_error(
                f"Run metadata records unknown profiler {value!r}",
                code="project_resume_configuration_invalid",
                stage="resume_resolution",
            )
    setattr(args, destination, value)


def _restore_project_resume_cli_args(
    args: argparse.Namespace,
    recorded: RunConfiguration,
    *,
    loop_kind: str,
) -> None:
    """Restore omitted run flags and reject explicit changes on resume."""
    if recorded.outer_loop != loop_kind:
        _configuration_error(
            f"Run uses --outer-loop {recorded.outer_loop}, not {loop_kind}",
            code="project_resume_configuration_mismatch",
            stage="resume_resolution",
        )

    explicit = getattr(args, "explicit_cli_dests", frozenset())
    fields, changed = _restore_loop_resume_fields(args, recorded, explicit)

    for destination, field in fields.items():
        if not hasattr(args, destination):
            continue
        expected = getattr(recorded, field)
        if destination in explicit:
            requested = _normalized_resume_cli_value(destination, getattr(args, destination))
            if requested != expected:
                changed.append(field)
            continue
        _set_resume_cli_value(args, destination, expected)

    if changed:
        _project_resume_mismatch(changed)


def _restore_loop_resume_fields(
    args: argparse.Namespace,
    recorded: RunConfiguration,
    explicit: frozenset[str],
) -> tuple[dict[str, str], list[str]]:
    """Restore a loop's budget and return its immutable CLI field map."""
    fields = dict(_COMMON_RESUME_CLI_FIELDS)
    changed: list[str] = _restore_resume_run_environment(args, recorded, explicit)
    if isinstance(recorded, AgentRunConfiguration):
        _restore_resume_budget(
            args,
            destination="max_rounds",
            recorded_value=recorded.max_rounds,
            explicit=explicit,
        )
        fields.update(_AGENT_RESUME_CLI_FIELDS)
        if _restore_resume_agent_backend(args, recorded, explicit):
            changed.append("agent_backend")
        if _restore_resume_constraints(args, recorded, explicit):
            changed.append("operator_constraints")
        fields.pop("agent_backend", None)
    elif isinstance(recorded, PlainRunConfiguration):
        _restore_resume_budget(
            args,
            destination="max_rounds",
            recorded_value=recorded.max_rounds,
            explicit=explicit,
        )
        fields.update(_PLAIN_RESUME_CLI_FIELDS)
    else:
        _restore_resume_budget(
            args,
            destination="max_generations",
            recorded_value=recorded.max_generations,
            explicit=explicit,
        )
        fields.update(_EVOLVE_RESUME_CLI_FIELDS)
        if "objective" in explicit:
            requested = tuple(f"{item.name}:{item.direction}" for item in args.objective)
            if requested != recorded.objectives:
                changed.append("objectives")
        else:
            args.objective = [_parse_cli_objective(item) for item in recorded.objectives]
    return fields, changed


def _switch_project_resume_branch(project_root: Path, run_id: str) -> None:
    """Select the run branch before callers read committed project files."""
    # Store-only fixtures and corrupt partial initializations are diagnosed by
    # context creation. Real runs always have a repository here.
    if not (project_root / ".git").exists():
        return
    tracker = GitTracker(
        project_root,
        log=lambda _message: None,
        run_id=run_id,
    )
    try:
        tracker.init(existing=True)
    except (subprocess.SubprocessError, ValueError) as exc:
        _configuration_error(
            f"Cannot resume project run {run_id!r}: {exc}",
            code="resume_not_found",
            stage="resume_resolution",
        )


def _resolve_resume_args(args: argparse.Namespace, *, loop_kind: str) -> None:
    if args.resume is None:
        return
    if args.repo is not None:
        _configuration_error(
            "--repo creates a remote for a new experiment and cannot be combined with --resume",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    direct = args.runs_dir is None
    project_root = (
        (args.input or Path.cwd()).expanduser().resolve()
        if direct
        else _resolve_project_root(args.resume, args.runs_dir)
    )
    try:
        store = Project.open(project_root).state
        if direct and args.resume != "latest":
            run_id = args.resume
        else:
            run_id = store.current_run_id()
            if run_id is None:
                run_id = store.resolve_run().run_id
        _switch_project_resume_branch(project_root, run_id)
        run_manifest = store.load_run(run_id)
    except RunSchemaMigrationRequiredError as exc:
        _configuration_error(
            f"{exc} Run: {_migrate_run_environment_command(project_root, exc.run_id)}",
            code="project_run_schema_migration_required",
            stage="resume_resolution",
        )
    except ProjectStateError as exc:
        _configuration_error(
            f"Cannot resume project run: {exc}",
            code="resume_not_found",
            stage="resume_resolution",
        )
    args.resume = run_id
    args.exp_name = run_id
    args.input = project_root
    if run_manifest.task_name is not None:
        if args.task is not None and args.task != run_manifest.task_name:
            _configuration_error(
                f"Run uses task {run_manifest.task_name!r}, not {args.task!r}",
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
            )
        args.task = run_manifest.task_name
    args.project_run_configuration = run_manifest.configuration
    _restore_project_resume_cli_args(args, run_manifest.configuration, loop_kind=loop_kind)


def _apply_common_args(parser: argparse.ArgumentParser) -> None:
    """Common args + the cross-loop ``--resume`` flag."""
    _add_common_args(parser)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Disable the interactive client even when attached to a terminal.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="RUN",
        help="Resume a run; omit RUN to select the current or latest run.",
    )


def _make_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = _RunArgumentParser(prog=prog, description=description)
    _apply_common_args(parser)
    return parser


# ---------------------------------------------------------------------------
# Run metadata migration command
# ---------------------------------------------------------------------------

_MIGRATE_RUN_ENVIRONMENT_COMMAND = "migrate-run-environment"


def _migrate_run_environment_command(project_root: Path, run_id: str) -> str:
    """Render the operator command that migrates one run's recorded metadata."""
    return shlex.join(
        [
            "vibesys",
            _MIGRATE_RUN_ENVIRONMENT_COMMAND,
            "--project",
            str(project_root),
            "--run",
            run_id,
            "--run-environment",
            "local|docker|modal",
        ]
    )


def _build_migrate_run_environment_parser() -> argparse.ArgumentParser:
    parser = _RunArgumentParser(
        prog=f"vibesys {_MIGRATE_RUN_ENVIRONMENT_COMMAND}",
        description=(
            "Migrate an existing run's execution metadata. Version 1 never "
            "captured the runtime environment, so the operator supplies it. "
            "For version 2, the supplied environment must match the recording "
            "before portable resource metadata is added. The migration is one-way."
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="Project directory holding the run metadata. Defaults to the current directory.",
    )
    parser.add_argument(
        "--run",
        default=None,
        help="Run ID to migrate. Defaults to the project's current run.",
    )
    parser.add_argument(
        "--run-environment",
        choices=["local", "docker", "modal"],
        required=True,
        help="Runtime environment the run was launched with.",
    )
    # These mirror the run flags, defaults included, so a migrated recording is
    # identical to what the same launch would have written today.
    parser.add_argument("--docker-image", default=None, help="Recorded --docker-image value.")
    parser.add_argument("--modal-gpu", default="H100!", help="Recorded --modal-gpu value.")
    parser.add_argument(
        "--modal-model-volume",
        default=None,
        help="Recorded --modal-model-volume value.",
    )
    parser.add_argument("--modal-app", default="vibesys", help="Recorded --modal-app value.")
    return parser


def _run_migrate_run_environment(argv: list[str]) -> None:
    """Migrate one version 1 or 2 run to the current execution schema."""
    args = _build_migrate_run_environment_parser().parse_args(argv)
    project_root = (args.project or Path.cwd()).expanduser().resolve()
    # Build the record through the same producer a fresh run uses so a migrated
    # recording matches what the CLI would have written for those flags.
    spec = make_run_environment_spec(
        use_docker=args.run_environment == "docker",
        use_modal=args.run_environment == "modal",
        docker_image=args.docker_image,
        modal_gpu=args.modal_gpu,
        modal_model_volume=args.modal_model_volume,
        modal_app=args.modal_app,
    )
    try:
        store = Project.open(project_root).state
        run_id = args.run or store.current_run_id()
        if run_id is None:
            _configuration_error(
                f"No current run in {project_root}; pass --run RUN_ID",
                code="migration_failed",
                stage="run_migration",
                exit_code=1,
            )
        manifest = store.migrate_run_environment(run_id, run_environment_record(spec))
    except (ProjectLayoutError, ProjectStateError, ValueError) as exc:
        _configuration_error(
            f"Migration failed for VibeSys run in {project_root}: {exc}",
            code="migration_failed",
            stage="run_migration",
            exit_code=1,
        )

    print(  # noqa: T201  # tracked: #288
        f"Migrated run {manifest.run_id} to run schema version "
        f"{manifest.schema_version}: run environment "
        f"{manifest.configuration.run_environment.name}"
    )
    print(f"  metadata: {project_root}")  # noqa: T201  # tracked: #288
    print("  commit the updated run metadata to keep the run branch clean.")  # noqa: T201  # tracked: #288


# ---------------------------------------------------------------------------
# Input-bundle validation command
# ---------------------------------------------------------------------------


def _build_validate_parser() -> argparse.ArgumentParser:
    parser = _RunArgumentParser(
        prog="vibesys validate",
        description="Validate a repository-native VibeSys task without starting a run.",
    )
    parser.add_argument(
        "project",
        type=Path,
        nargs="?",
        default=None,
        help="Path to the candidate repository (default: current directory).",
    )
    parser.add_argument(
        "--task",
        default=None,
        metavar="NAME",
        help="Task name; optional when the project defines exactly one task.",
    )
    return parser


def _run_validate(argv: list[str]) -> None:
    """Validate one input-bundle contract, then report its resolved paths."""
    args = _build_validate_parser().parse_args(argv)
    input_path = (args.project or Path.cwd()).expanduser().resolve()

    try:
        bundle = _load_selected_input(input_path, args.task)
    except (FileNotFoundError, ProjectLayoutError, ValueError) as exc:
        _configuration_error(
            f"Validation failed for VibeSys project {input_path}: {exc}",
            code="validation_failed",
            stage="input_validation",
            exit_code=1,
        )

    print("VibeSys validation passed: task is valid.")  # noqa: T201  # tracked: #288
    print(f"  project: {bundle.root}")  # noqa: T201  # tracked: #288
    if bundle.task_name is not None:
        print(f"  task: {bundle.task_name}")  # noqa: T201  # tracked: #288
    print(f"  objective: {bundle.objective_path}")  # noqa: T201  # tracked: #288
    print(f"  accuracy command: {bundle.accuracy_command_display}")  # noqa: T201  # tracked: #288
    print(f"  benchmark command: {bundle.benchmark_command_display}")  # noqa: T201  # tracked: #288
    for source in bundle.workspace_sources:
        print(f"  workspace source: {source.name} -> {source.dest} @ {source.commit}")  # noqa: T201  # tracked: #288
    if bundle.evaluator_path is not None:
        print(f"  evaluator source: {bundle.evaluator_path}")  # noqa: T201  # tracked: #288
    if bundle.benchmark_result is not None:
        print(f"  benchmark metric: {bundle.benchmark_result.metric}")  # noqa: T201  # tracked: #288
    if bundle.benchmark_result_protocol is not None:
        print(  # noqa: T201  # tracked: #288
            f"  benchmark result protocol: {bundle.benchmark_result_protocol}"
        )


# ---------------------------------------------------------------------------
# Shared input-bundle discovery
# ---------------------------------------------------------------------------


def _load_selected_input(project_root: Path, task_name: str | None) -> InputBundle:
    """Select repository-native input, with legacy bundle compatibility."""
    project = Project.open(project_root)
    if project.is_initialized():
        return load_project_task(project, project.select_task(task_name))
    if task_name is not None:
        raise ValueError("--task requires a project with .vibesys/tasks")  # noqa: TRY003
    return load_input_bundle(project_root)


def _load_objective(bundle: InputBundle) -> str:
    """Return the input bundle's objective text."""
    return bundle.objective


def _with_operator_constraints(objective: str, constraints: list[str]) -> str:
    """Add run-specific invariants without mutating the input bundle."""
    normalized = [constraint.strip() for constraint in constraints if constraint.strip()]
    if not normalized:
        return objective
    lines = "\n".join(f"- {constraint}" for constraint in normalized)
    return f"{objective.rstrip()}\n\n## Operator constraints\n\n{lines}\n"


# ===========================================================================
# agent loop  (--outer-loop agent)
# ===========================================================================


def _build_agent_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibesys --outer-loop agent",
        description="Run the autonomous orchestrator-driven build loop.",
    )
    parser.add_argument("--max-rounds", type=int, default=24)
    parser.add_argument("--max-retries-per-round", type=int, default=3)
    parser.add_argument(
        "--constraint",
        action="append",
        default=[],
        metavar="TEXT",
        help=(
            "Add a run-specific invariant to every agent's workload objective. "
            "Repeat the flag for multiple constraints."
        ),
    )
    parser.add_argument(
        "--judge-every",
        type=int,
        default=3,
        metavar="N",
        help=(
            "In the multi-agent loop, run independent review every N rounds; "
            "nominated candidates and the final round are always reviewed (default: 3)."
        ),
    )
    parser.add_argument(
        "--official-eval-every",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Run configured framework-owned accuracy/benchmark gates every N "
            "accepted candidate checkpoints; orchestrator requests and the final "
            "round run them immediately (default: 3)."
        ),
    )
    parser.add_argument(
        "--memory-layout",
        choices=["files", "directories"],
        default="files",
        help=(
            "Store roadmap/progress as roadmap.md + progress.md (files), or as "
            "roadmap/index.md + progress/round-NNNN.md (directories)."
        ),
    )
    parser.add_argument(
        "--stub-agent",
        action="store_true",
        help="Use deterministic local agent responses for fast TUI smoke tests.",
    )
    parser.add_argument("--modality", default=None, choices=_MODALITIES)
    parser.add_argument(
        "--interface",
        choices=["inprocess", "service"],
        default="inprocess",
        help=(
            "Process boundary used by the evaluator. 'inprocess' (default): "
            "the evaluator invokes the candidate directly using the input-defined "
            "contract. 'service': the evaluator communicates with a running "
            "service over its network interface. Language, tooling, and artifact "
            "requirements come from the selected domain and input bundle."
        ),
    )
    parser.add_argument(
        "--inner-loop",
        choices=["multi-agent", "single-agent"],
        default="multi-agent",
        help=(
            "How to dispatch implement/judge/profile work each round. "
            "'multi-agent' (default) uses three specialist agents. "
            "'single-agent' (ablation) uses one agent for all three roles."
        ),
    )
    return parser


def _standalone_input_dests_set(args: argparse.Namespace) -> list[str]:
    """Return the standalone-input flag dests that were provided (non-default)."""
    return [dest for dest in _STANDALONE_INPUT_DESTS if getattr(args, dest, None) is not None]


def _resolve_standalone_objective(args: argparse.Namespace) -> str:
    if args.input_objective is not None and args.input_objective_file is not None:
        _configuration_error(
            "Error: pass only one of --input-objective or --input-objective-file.",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if args.input_objective is not None:
        return args.input_objective
    if args.input_objective_file is None:
        _configuration_error(
            "Error: --input-objective or --input-objective-file is required.",
            code="missing_input",
            stage="input_loading",
        )
    objective_file = args.input_objective_file.expanduser()
    if not objective_file.is_file():
        _configuration_error(
            f"Error: --input-objective-file not found: {args.input_objective_file}",
            code="invalid_input",
            stage="input_loading",
        )
    return objective_file.read_text()


def _parse_command_flag(raw: str, flag: str) -> tuple[str, ...]:
    try:
        parts = tuple(shlex.split(raw))
    except ValueError as exc:
        _configuration_error(
            f"Error: could not parse {flag}: {exc}",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    if not parts:
        _configuration_error(
            f"Error: {flag} must contain at least one argument.",
            code="invalid_arguments",
            stage="argument_parsing",
        )
    return parts


def _synthesize_standalone_input(args: argparse.Namespace) -> Path:
    """Materialize standalone-input flags into a bundle and return its path."""
    from vibesys.input_synthesis import (  # noqa: PLC0415  # tracked: #288
        InputSynthesisError,
        SynthesizedInputSpec,
        synthesize_input_bundle,
    )

    missing = [
        flag
        for flag, present in (
            (
                "--input-objective/--input-objective-file",
                args.input_objective is not None or args.input_objective_file is not None,
            ),
            ("--input-domain", args.input_domain is not None),
            ("--input-accuracy-command", args.input_accuracy_command is not None),
            ("--input-benchmark-command", args.input_benchmark_command is not None),
        )
        if not present
    ]
    if missing:
        _configuration_error(
            "Error: standalone input requires " + ", ".join(missing) + ".",
            code="missing_input",
            stage="input_loading",
        )

    objective = _resolve_standalone_objective(args)
    spec = SynthesizedInputSpec(
        objective=objective,
        domain=args.input_domain,
        accuracy_command=_parse_command_flag(
            args.input_accuracy_command, "--input-accuracy-command"
        ),
        benchmark_command=_parse_command_flag(
            args.input_benchmark_command, "--input-benchmark-command"
        ),
        accuracy_timeout_seconds=args.input_accuracy_timeout,
        benchmark_timeout_seconds=args.input_benchmark_timeout,
        benchmark_metric=args.input_benchmark_metric,
        benchmark_result_arg=args.input_benchmark_result_arg,
        reference_dir=args.input_reference,
        evaluator_dir=args.input_evaluator_dir,
        evaluator_source_dir=args.input_evaluator_source,
    )

    if args.exp_name is None:
        args.exp_name = generate_experiment_name(Path(str(args.input_domain)))
    destination = args.runs_dir / "_inputs" / args.exp_name
    try:
        return synthesize_input_bundle(spec, destination)
    except InputSynthesisError as exc:
        _configuration_error(str(exc), code="invalid_input", stage="input_loading")


def _validate_target_inputs(args: argparse.Namespace) -> None:
    if args.resume is None and args.exp_name is not None:
        try:
            validate_experiment_name(args.exp_name)
        except ValueError as exc:
            _configuration_error(
                str(exc),
                code="invalid_exp_name",
                stage="argument_parsing",
            )

    input_arg = getattr(args, "input", None)
    task_name = getattr(args, "task", None)
    standalone = _standalone_input_dests_set(args)

    if input_arg is not None and standalone:
        _configuration_error(
            "Error: --input cannot be combined with standalone input flags "
            f"({', '.join('--' + dest.replace('_', '-') for dest in standalone)}).",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    if input_arg is None:
        input_arg = _resolve_implicit_input(args, standalone)
        args.input = input_arg

    try:
        args.input_bundle = _load_selected_input(input_arg, task_name)
        args.task = args.input_bundle.task_name
    except (FileNotFoundError, ProjectLayoutError, ValueError) as exc:
        _configuration_error(str(exc), code="invalid_input", stage="input_loading")

    _apply_bundle_profiler_default(args)

    if args.resume is None and args.runs_dir is None:
        workspace = args.input_bundle.manifest.workspace
        if workspace is not None and workspace.sources:
            _configuration_error(
                "A direct project run requires candidate source at the project root. "
                "Use --runs-dir to provision [workspace] inputs into a copied project.",
                code="direct_project_materialization_unsupported",
                stage="input_validation",
            )


def _apply_bundle_profiler_default(args: argparse.Namespace) -> None:
    """Upgrade ``--profiler auto`` to OTel when the task provisions tracing.

    ``resolve_profiler_kind`` keeps bare microservice ``auto`` on ``none``
    because OTel needs instrumentation and a collector that only the task can
    provide. A benchmark command that emits a normalized telemetry report and a
    trace graph has provided exactly that, so honoring it here is what makes an
    instrumented task profile out of the box. An explicit ``--profiler`` always
    wins: this only ever replaces ``auto``.
    """
    bundle = getattr(args, "input_bundle", None)
    if bundle is None or getattr(args, "profiler", None) is not ProfilerKind.AUTO:
        return
    if bundle.domain is not DomainName.MICROSERVICES:
        return
    if not bundle.provisions_trace_telemetry:
        return
    args.profiler = ProfilerKind.OTEL


def _resolve_implicit_input(args: argparse.Namespace, standalone: list[str]) -> Path:
    """Resolve cwd project discovery or the legacy standalone synthesizer."""
    if standalone:
        return _synthesize_standalone_input(args)
    current = Path.cwd().resolve()
    legacy_markers = (current / "OBJECTIVE.md", current / "vibesys.input.toml")
    repository_native = Project.open(current).is_initialized()
    if repository_native or all(marker.is_file() for marker in legacy_markers):
        return current
    missing = ", ".join(marker.name for marker in legacy_markers if not marker.is_file())
    _configuration_error(
        "Current directory is not a VibeSys project "
        f"({current}; missing .vibesys/tasks or legacy {missing}). "
        "Launch VibeSys from the project or pass --project PATH.",
        code="missing_input",
        stage="input_loading",
    )


def _validate_agent(args: argparse.Namespace) -> None:
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)
    if args.max_retries_per_round < 1:
        _configuration_error("Error: --max-retries-per-round must be >= 1.")
    if args.judge_every < 1:
        _configuration_error("Error: --judge-every must be >= 1.")
    if args.official_eval_every < 1:
        _configuration_error("Error: --official-eval-every must be >= 1.")


def _run_agent(args: argparse.Namespace, integration: RunIntegration) -> None:
    # Everything the run needs before the loop can start. The enclosing span
    # is this preamble's total; ``context.py`` times assembly from there.
    with boot_trace.span("agent_preamble"):
        bundle: InputBundle = args.input_bundle
        with boot_trace.span("load_config_and_skills"):
            config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
        with boot_trace.span("prepare_experiment_repository"):
            _prepare_experiment_repository(args, config)
        with boot_trace.span("import_run_agent_loop"):
            from vibesys.loops.agent.loop import run_agent_loop  # noqa: PLC0415  # tracked: #288
        with boot_trace.span("load_objective"):
            objective = _with_operator_constraints(_load_objective(bundle), args.constraint)

        existing = False
        exp_name = args.exp_name
        start_round = 1

        if args.resume is not None:
            exp_name = args.resume
            existing = True
            start_round = None
            print(f"Resuming VibeSys run {exp_name} in {bundle.root}/")  # noqa: T201  # tracked: #288

        with boot_trace.span("load_objectives_toml"):
            metrics = _load_metric_space_toml(bundle.task_root)

        with boot_trace.span("run_environment_spec"):
            run_environment = run_environment_spec_from_args(
                args,
                build_task_docker_image=True,
            )

    success = run_agent_loop(
        config=config,
        exp_name=exp_name,
        runs_dir=args.runs_dir,
        input_path=str(bundle.root),
        task_name=bundle.task_name,
        task_root=bundle.task_root,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_sources=bundle.workspace_sources,
        evaluator_path=bundle.evaluator_path,
        evaluator_package_root=bundle.evaluator_package_root,
        benchmark_result=bundle.benchmark_result,
        benchmark_result_protocol=bundle.benchmark_result_protocol,
        accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
        benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        objective=objective,
        metrics=metrics,
        max_rounds=args.max_rounds,
        max_retries_per_round=args.max_retries_per_round,
        judge_every=args.judge_every,
        official_eval_every=args.official_eval_every,
        memory_layout=args.memory_layout,
        start_round=start_round,
        existing=existing,
        operator_constraints=tuple(
            constraint.strip() for constraint in args.constraint if constraint.strip()
        ),
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment,
        agent_backend="stub" if args.stub_agent else args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        domain=bundle.domain,
        interface=args.interface,
        inner_loop=args.inner_loop,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
        integration=integration,
    )

    if success:
        print(f"\nAgent loop completed {args.max_rounds} rounds.")  # noqa: T201  # tracked: #288
    else:
        print("\nAgent loop stopped early (exception or KeyboardInterrupt).")  # noqa: T201  # tracked: #288
        sys.exit(1)


# ===========================================================================
# evolve loop  (--outer-loop evolve)
# ===========================================================================


def _parse_cli_objective(spec: str) -> Objective:
    """Parse a ``--objective`` flag value (``name:direction``)."""
    if ":" not in spec:
        raise argparse.ArgumentTypeError(f"--objective {spec!r} must be 'name:max' or 'name:min'")  # noqa: TRY003  # tracked: #288
    name, _, direction = spec.partition(":")
    name = name.strip()
    direction = direction.strip().lower()
    if not name:
        raise argparse.ArgumentTypeError(f"--objective {spec!r}: metric name is empty")  # noqa: TRY003  # tracked: #288
    if direction not in ("max", "min"):
        raise argparse.ArgumentTypeError(  # noqa: TRY003  # tracked: #288
            f"--objective {spec!r}: direction must be 'max' or 'min', got {direction!r}"
        )
    return Objective(name=name, direction=direction)


def _load_metric_space_toml(input_path: Path) -> MetricSpace:
    """Read the run's metric space from the task's ``objectives.toml``.

    The axes and the measurement tolerance are one fact about the workload and
    are read together, once. ``run_agent_loop`` persists the returned space
    into the run state, and every consumer that has to order two readings takes
    it from there.

    Exact comparison remains the default. Inputs with measured benchmark
    variation can opt into a relative margin under ``[pareto]`` without
    imposing one domain's noise level on every optimization workload.
    """
    path = input_path / "objectives.toml"
    if not path.exists():
        return MetricSpace()
    data = tomllib.loads(path.read_text())
    objectives = []
    for entry in data.get("objective") or []:
        name = entry.get("name")
        direction = entry.get("direction")
        if not name or direction not in ("max", "min"):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"Malformed entry in {path}: {entry!r}. Each [[objective]] "
                f"must set name and direction (max|min)."
            )
        objectives.append(Objective(name=name, direction=direction))
    raw_value = (data.get("pareto") or {}).get("relative_noise", 0.0)
    if isinstance(raw_value, bool):
        raise ValueError(f"Malformed pareto.relative_noise in {path}: {raw_value!r}")  # noqa: TRY003, TRY004  # tracked: #288
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Malformed pareto.relative_noise in {path}: {raw_value!r}") from exc  # noqa: TRY003  # tracked: #288
    if not math.isfinite(value) or not 0 <= value < 1:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Malformed pareto.relative_noise in {path}: expected a finite value "
            f"in [0, 1), got {raw_value!r}"
        )
    return MetricSpace(objectives=tuple(objectives), relative_noise=value)


def _resolve_metric_space(args: argparse.Namespace) -> MetricSpace:
    """Build the run's metric space, letting ``--objective`` override its axes.

    The tolerance always comes from the task file: it describes the workload's
    measurement variation, which a command-line axis list does not change.
    """
    space = _load_metric_space_toml(args.input_bundle.task_root)
    if args.objective:
        return MetricSpace(
            objectives=tuple(args.objective),
            relative_noise=space.relative_noise,
        )
    return space


def _build_evolve_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibesys --outer-loop evolve",
        description="Run the evolutionary-search build loop.",
    )
    parser.add_argument("--max-generations", type=int, default=8)
    parser.add_argument("--children-per-generation", type=int, default=2)
    parser.add_argument("--k-top-inspirations", type=int, default=2)
    parser.add_argument("--k-random-inspirations", type=int, default=2)
    parser.add_argument("--selection-temperature", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--search-policy",
        choices=("vibesys", "openevolve"),
        default=None,
        help=(
            "Population selection policy (default: vibesys for new runs; restored "
            "from OpenEvolve state on resume)."
        ),
    )
    parser.add_argument(
        "--openevolve-population-size",
        type=int,
        default=None,
        help="Maximum upstream program population (OpenEvolve only; default: 1000).",
    )
    parser.add_argument(
        "--openevolve-archive-size",
        type=int,
        default=None,
        help="Maximum upstream elite archive size (OpenEvolve only; default: 100).",
    )
    parser.add_argument(
        "--openevolve-num-islands",
        type=int,
        default=None,
        help="Number of island populations (OpenEvolve only; default: 5).",
    )
    parser.add_argument(
        "--openevolve-migration-interval",
        type=int,
        default=None,
        help="Admitted island generations between migrations (default: 50).",
    )
    parser.add_argument(
        "--openevolve-migration-rate",
        type=float,
        default=None,
        help="Fraction of island elites migrated; 0 disables migration (default: 0.1).",
    )
    parser.add_argument(
        "--objective",
        action="append",
        default=[],
        type=_parse_cli_objective,
        metavar="NAME:DIRECTION",
    )
    parser.add_argument("--frontier-bias", type=float, default=0.7)
    parser.add_argument("--bootstrap-max-attempts", type=int, default=5)
    parser.add_argument(
        "--keep-deployments",
        "--keep-modal-apps",
        dest="keep_deployments",
        action="store_true",
        help=(
            "Do not tear down each candidate's environment-owned deployment "
            "after evaluation (default: release it). Keep deployments for "
            "post-hoc inspection. --keep-modal-apps is a compatibility alias."
        ),
    )
    parser.add_argument(
        "--max-parallelism",
        type=int,
        default=1,
        help=(
            "Max candidates to evaluate concurrently within a generation "
            "(default: 1 = serial). Values >1 take effect only when the selected "
            "run environment supports isolated candidate evaluation."
        ),
    )
    parser.add_argument("--modality", default=None, choices=_MODALITIES)
    return parser


def _validate_evolve(args: argparse.Namespace) -> None:  # noqa: C901  # tracked: #288
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)
    if args.children_per_generation < 1:
        _configuration_error("--children-per-generation must be >= 1.")
    if args.max_generations < 1:
        _configuration_error("--max-generations must be >= 1.")
    if args.selection_temperature <= 0:
        _configuration_error("--selection-temperature must be > 0.")
    if args.search_policy == "vibesys" and any(
        value is not None
        for value in (
            args.openevolve_population_size,
            args.openevolve_archive_size,
            args.openevolve_num_islands,
            args.openevolve_migration_interval,
            args.openevolve_migration_rate,
        )
    ):
        _configuration_error("--openevolve-* settings cannot be used with --search-policy vibesys.")
    if args.openevolve_population_size is not None and args.openevolve_population_size < 1:
        _configuration_error("--openevolve-population-size must be >= 1.")
    if args.openevolve_archive_size is not None and args.openevolve_archive_size < 1:
        _configuration_error("--openevolve-archive-size must be >= 1.")
    if args.openevolve_num_islands is not None and args.openevolve_num_islands < 1:
        _configuration_error("--openevolve-num-islands must be >= 1.")
    if args.openevolve_migration_interval is not None and args.openevolve_migration_interval < 1:
        _configuration_error("--openevolve-migration-interval must be >= 1.")
    if args.openevolve_migration_rate is not None and not (
        0.0 <= args.openevolve_migration_rate <= 1.0
    ):
        _configuration_error("--openevolve-migration-rate must be in [0, 1].")
    if not (0.0 <= args.frontier_bias <= 1.0):
        _configuration_error("--frontier-bias must be in [0, 1].")
    if args.bootstrap_max_attempts < 1:
        _configuration_error("--bootstrap-max-attempts must be >= 1.")
    if args.max_parallelism < 1:
        _configuration_error("--max-parallelism must be >= 1.")


def _resolve_openevolve_options(
    args: argparse.Namespace,
) -> tuple[str | None, OpenEvolveSearchConfig | None]:
    from vibesys.loops.evolve.search_policy import (  # noqa: PLC0415  # tracked: #288
        OpenEvolveSearchConfig,  # tracked: #288
    )

    openevolve_defaults = OpenEvolveSearchConfig()
    openevolve_values = (
        args.openevolve_population_size,
        args.openevolve_archive_size,
        args.openevolve_num_islands,
        args.openevolve_migration_interval,
        args.openevolve_migration_rate,
    )
    openevolve_config = (
        OpenEvolveSearchConfig(
            population_size=args.openevolve_population_size or openevolve_defaults.population_size,
            archive_size=args.openevolve_archive_size or openevolve_defaults.archive_size,
            num_islands=args.openevolve_num_islands or openevolve_defaults.num_islands,
            migration_interval=args.openevolve_migration_interval
            or openevolve_defaults.migration_interval,
            migration_rate=(
                args.openevolve_migration_rate
                if args.openevolve_migration_rate is not None
                else openevolve_defaults.migration_rate
            ),
        )
        if any(value is not None for value in openevolve_values)
        else None
    )
    search_policy = args.search_policy or ("openevolve" if openevolve_config is not None else None)
    return search_policy, openevolve_config


def _run_evolve(args: argparse.Namespace, integration: RunIntegration) -> None:
    bundle: InputBundle = args.input_bundle
    config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
    _prepare_experiment_repository(args, config)
    from vibesys.loops.evolve.loop import run_evolve_loop  # noqa: PLC0415  # tracked: #288

    objective = _load_objective(bundle)
    space = _resolve_metric_space(args)

    existing = False
    exp_name = args.exp_name
    if args.resume is not None:
        exp_name = args.resume
        existing = True
        print(f"Resuming evolve run {exp_name} in {bundle.root}/")  # noqa: T201  # tracked: #288
    if space.objectives:
        spec = ", ".join(f"{o.name}({o.direction})" for o in space.objectives)
        print(  # noqa: T201  # tracked: #288
            f"Pareto mode active: [{spec}]; frontier_bias={args.frontier_bias}; "
            f"tolerance={space.relative_noise:.0%}"
        )

    search_policy, openevolve_config = _resolve_openevolve_options(args)

    success = run_evolve_loop(
        config=config,
        exp_name=exp_name,
        runs_dir=args.runs_dir,
        input_path=str(bundle.root),
        task_name=bundle.task_name,
        task_root=bundle.task_root,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_sources=bundle.workspace_sources,
        evaluator_path=bundle.evaluator_path,
        evaluator_package_root=bundle.evaluator_package_root,
        accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
        benchmark_result=bundle.benchmark_result,
        benchmark_result_protocol=bundle.benchmark_result_protocol,
        benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        objective=objective,
        max_generations=args.max_generations,
        children_per_generation=args.children_per_generation,
        k_top_inspirations=args.k_top_inspirations,
        k_random_inspirations=args.k_random_inspirations,
        selection_temperature=args.selection_temperature,
        seed=args.seed,
        existing=existing,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args, build_task_docker_image=True),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        domain=bundle.domain,
        space=space,
        frontier_bias=args.frontier_bias,
        bootstrap_max_attempts=args.bootstrap_max_attempts,
        keep_deployments=args.keep_deployments,
        max_parallelism=args.max_parallelism,
        search_policy=search_policy,
        openevolve_config=openevolve_config,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
        integration=integration,
    )

    if success:
        print(  # noqa: T201  # tracked: #288
            f"\nEvolve loop completed {args.max_generations} generations "
            f"× {args.children_per_generation} cands."  # noqa: RUF001  # tracked: #288
        )
    else:
        print("\nEvolve loop stopped early (exception or KeyboardInterrupt).")  # noqa: T201  # tracked: #288
        sys.exit(1)


# ===========================================================================
# plain loop  (--outer-loop plain)
# ===========================================================================


def _build_plain_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibesys --outer-loop plain",
        description=(
            "Run issue-tracker driven loop: perf_eval files issues, "
            "implementer drains them one at a time."
        ),
    )
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--max-attempts-per-issue", type=int, default=3)
    parser.add_argument("--max-issues-per-perf-eval", type=int, default=3)
    return parser


def _validate_plain(args: argparse.Namespace) -> None:
    _validate_target_inputs(args)
    _validate_run_environment_profiler(args)


def _run_plain(args: argparse.Namespace, integration: RunIntegration) -> None:
    bundle: InputBundle = args.input_bundle
    config, skills, backend = load_config_and_skills(args, domain=bundle.domain)
    _prepare_experiment_repository(args, config)
    from vibesys.loops.plain.loop import run_plain_loop  # noqa: PLC0415  # tracked: #288

    existing = False
    exp_name = args.exp_name
    if args.resume is not None:
        exp_name = args.resume
        existing = True
        print(f"Resuming plain run {exp_name} in {bundle.root}/")  # noqa: T201  # tracked: #288

    success = run_plain_loop(
        config=config,
        exp_name=exp_name,
        runs_dir=args.runs_dir,
        input_path=str(bundle.root),
        task_name=bundle.task_name,
        task_root=bundle.task_root,
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_sources=bundle.workspace_sources,
        evaluator_path=bundle.evaluator_path,
        evaluator_package_root=bundle.evaluator_package_root,
        max_rounds=args.max_rounds,
        max_attempts_per_issue=args.max_attempts_per_issue,
        max_issues_per_perf_eval=args.max_issues_per_perf_eval,
        existing=existing,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args, build_task_docker_image=True),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        domain=bundle.domain,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
        integration=integration,
    )

    if success:
        print("\nPlain loop completed: no remaining open issues.")  # noqa: T201  # tracked: #288
    else:
        print(f"\nPlain loop did not complete after {args.max_rounds} rounds.")  # noqa: T201  # tracked: #288
        sys.exit(1)


# ===========================================================================
# Dispatch
# ===========================================================================


@dataclass(frozen=True)
class _LoopCommand:
    """Typed dispatch record for one ``--outer-loop`` kind."""

    build_parser: Callable[[], argparse.ArgumentParser]
    validate: Callable[[argparse.Namespace], None]
    run: Callable[[argparse.Namespace, RunIntegration], None]


_LOOP_COMMANDS: dict[str, _LoopCommand] = {
    "agent": _LoopCommand(_build_agent_parser, _validate_agent, _run_agent),
    "plain": _LoopCommand(_build_plain_parser, _validate_plain, _run_plain),
    "evolve": _LoopCommand(_build_evolve_parser, _validate_evolve, _run_evolve),
}


def _explicit_cli_dests(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> frozenset[str]:
    """Return parser destinations whose option strings occur in *argv*."""
    destinations = {
        option: action.dest
        for action in parser._actions  # noqa: SLF001  # argparse exposes no public lookup
        for option in action.option_strings
    }
    return frozenset(
        destinations[option]
        for token in argv
        if token.startswith("-") and (option := token.partition("=")[0]) in destinations
    )


def parse_cli_invocation(argv: list[str]) -> CliInvocation:
    """Parse and validate one invocation without printing or exiting."""
    argv = _prepare_stub_agent_smoke_defaults(argv)
    loop_kind, remaining = _extract_loop_selection(argv)
    command = _LOOP_COMMANDS[loop_kind]
    parser = command.build_parser()
    args = parser.parse_args(remaining)
    args.explicit_cli_dests = _explicit_cli_dests(parser, remaining)
    _normalize_runs_dir(args)
    _resolve_resume_args(args, loop_kind=loop_kind)
    command.validate(args)
    return CliInvocation(loop_kind=loop_kind, args=args)


def dispatch(argv: list[str], integration: RunIntegration | None = None) -> None:
    """Parse and run one headless VibeSys invocation."""
    if argv and argv[0] == "validate":
        _run_validate(argv[1:])
        return
    if argv and argv[0] == _MIGRATE_RUN_ENVIRONMENT_COMMAND:
        _run_migrate_run_environment(argv[1:])
        return

    # Dispatch's own share of boot; the selected command's preamble
    # (``_run_agent``) and then ``context.py`` continue the trace. These
    # lines stay buffered until run-context assembly drains them.
    with boot_trace.span("dispatch"):
        with boot_trace.span("parse_cli_invocation"):
            invocation = parse_cli_invocation(argv)
        loop_kind, args = invocation.loop_kind, invocation.args
        runner = _LOOP_COMMANDS[loop_kind].run
    owns_integration = integration is None
    run_integration = integration or LocalRunIntegration()
    unsubscribe: Callable[[], None] | None = None
    try:
        if owns_integration:
            unsubscribe = run_integration.events.subscribe(HeadlessRenderer().handle)
        with boot_trace.span("run_started_event"):
            max_rounds = getattr(args, "max_rounds", getattr(args, "max_iterations", 1))
            expected_roles = expected_agent_roles(loop_kind)
            if args.profiler is ProfilerKind.NONE:
                # A disabled profiler never runs (see loop.py's profiler-kind
                # gate), so don't seed a placeholder for it.
                expected_roles = tuple(role for role in expected_roles if role != "profiler")
            run_integration.events.emit(
                CoreEventType.RUN_STARTED,
                status=EventStatus.ACTIVE,
                data=RunStartedData(
                    outer_loop=loop_kind,
                    input=str(args.input_bundle.root),
                    max_rounds=max_rounds,
                    expected_roles=expected_roles,
                ),
            )
        runner(args, run_integration)
    except BaseException as exc:
        run_integration.events.emit(
            CoreEventType.RUN_FAILED,
            f"{type(exc).__name__}: {exc}",
            status=EventStatus.FAILED,
        )
        raise
    else:
        run_integration.events.emit(
            CoreEventType.RUN_FINISHED,
            status=EventStatus.COMPLETED,
        )
    finally:
        if unsubscribe is not None:
            unsubscribe()
        if owns_integration:
            run_integration.close()


def _option_from_argv(argv: list[str], option: str) -> str | None:
    """Read one option's value without parsing the full run configuration."""
    prefix = f"{option}="
    for index, token in enumerate(argv):
        if token.startswith(prefix):
            return token[len(prefix) :]
        if token == option and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _render_configuration_error(error: ConfigurationError) -> NoReturn:
    diagnostic = error.diagnostic
    print(f"vibesys: {diagnostic.message}", file=sys.stderr)  # noqa: T201  # tracked: #288
    if diagnostic.usage:
        print(diagnostic.usage, file=sys.stderr)  # noqa: T201  # tracked: #288
    raise SystemExit(diagnostic.exit_code)


def main(argv: list[str] | None = None) -> None:  # tracked: #288
    """Run the headless command-line interface."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        dispatch(arguments)
    except ConfigurationError as exc:
        _render_configuration_error(exc)


if __name__ == "__main__":
    main()
