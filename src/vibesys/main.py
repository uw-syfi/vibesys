"""Python backend entry point for VibeSys runs.

The loop is picked by ``--outer-loop {agent, plain, evolve}``:

  "agent"  — an LLM Orchestrator decides per-round what to build next.
             Its issue board lives in the workspace as roadmap.md +
             progress.md, owned by the orchestrator.
  "plain"  — deterministic outer loop. Its issue board is a structured
             :class:`IssueBoard` (issues.json) that perf_eval files into
             and the implementer drains one issue at a time.
  "evolve" — population-based evolutionary search.

The TypeScript launcher owns interactive orchestration. This module owns
Python-side argument parsing, validation, server supervision, and loop dispatch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from vibesys.config import Config, load_config
from vibesys.constants import (
    KNOWN_COMPUTE_BACKENDS,
    PROJECT_ROOT,
    ComputeBackend,
)
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.input_manifest import InputBundle, load_input_bundle
from vibesys.profilers import CLI_PROFILER_CHOICES, ProfilerKind, coerce_profiler_kind
from vibesys.repository import (
    REPOSITORY_SLUG,
    InteractiveSetupDefaults,
    RepositoryVisibility,
    generate_experiment_name,
    repository_name_from_experiment,
)
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)
from vibesys.skills import DEFAULT_SKILL_ROOTS, resolve_skill_source_dirs
from vs_github import GitHubCLI, GitHubCLIError

if TYPE_CHECKING:
    from vibesys.loops.evolve.population import Objective

_OUTER_LOOPS = ("agent", "plain", "evolve")
_MODALITIES = (
    "text_generation",
    "image_generation",
    "video_generation",
    "text_to_speech",
    "speech_to_text",
    "realtime_audio",
    "kv_store",
)

_MODAL_PROFILERS = frozenset({ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE})
_STUB_AGENT_DEFAULT_INPUT = PROJECT_ROOT / "examples" / "data-structures" / "queue-spsc"
_STUB_AGENT_DEFAULT_CONFIG_TEXT = '[model]\nname = "gpt-5.5"\n'


class _RunArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports errors through the supervision protocol."""

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
class CliInvocation:
    loop_kind: str
    args: argparse.Namespace


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


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add CLI arguments shared across every outer-loop parser."""
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Path to a target input bundle containing OBJECTIVE.md and "
            "vibesys.input.toml with accuracy and benchmark commands."
        ),
    )
    parser.add_argument(
        "--exp-name",
        required=False,
        default="test",
        help="Experiment name (creates exp_env/<name>/)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "agent.toml",
        help="Path to agent TOML config file (default: agent.toml)",
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
            "'torch' for torch.profiler (works in Modal sandboxes), "
            "'neuron' for AWS neuron-explorer (Trainium/NeuronCores), "
            "'macos_cpu' for Instruments Time Profiler with a sample fallback, "
            "'linux_cpu' for Linux perf on native CPU workloads, "
            "'auto' picks a domain/backend/environment-appropriate profiler. "
            "Default: auto."
        ),
    )
    parser.add_argument(
        "--skills-dir",
        default=list(DEFAULT_SKILL_ROOTS),
        action="append",
        type=Path,
        help=(
            "Path to a skill source candidate root (can be repeated). Each "
            "entry can be either a single skill directory (containing a "
            "top-level `SKILL.md`) or a parent directory of multiple skill "
            "directories. Skills with `vibesys.backends` metadata are loaded "
            "only for matching --backend values. Default: `resources/skills/`."
        ),
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help=(
            "Disable skills entirely: no skill directories are copied into "
            "the workspace and no per-CLI skill-discovery paths are populated. "
            "Used for ablations measuring the skill library's contribution. "
            "Overrides --skills-dir."
        ),
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run agent operations inside a Docker container.",
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
            "is dispatched via `modal run`. Mutually exclusive with --docker."
        ),
    )
    parser.add_argument(
        "--modal-gpu",
        type=str,
        default="H100",
        help=(
            "Default Modal GPU spec for `@app.function` / `@app.cls` decorators "
            "(e.g. H100, A100, L40S). Default: H100."
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
        "--debug",
        action="store_true",
        help="Pause for Enter at each step in loop mode.",
    )
    parser.add_argument(
        "--git-tracking",
        action="store_true",
        help="Track workspace versions via git commits instead of directory snapshots.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="[OWNER/]NAME",
        help=(
            "Create a GitHub repository for this experiment, commit its durable "
            "state, and push after each run. A configured [repository].owner supplies "
            "an omitted owner. Requires an authenticated `gh` CLI."
        ),
    )
    parser.add_argument(
        "--repo-visibility",
        type=RepositoryVisibility,
        choices=list(RepositoryVisibility),
        default=None,
        help=(
            "Visibility for a repository created by --repo. Defaults to "
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
        choices=["claude", "gemini", "codex", "opencode"],
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


def load_config_and_skills(
    args: argparse.Namespace,
) -> tuple[Config, list[str] | None, ComputeBackend]:
    """Load config from args.config, process skills_dir, and resolve the backend."""
    if getattr(args, "stub_agent", False) and not Path(args.config).is_file():
        config = Config.model_validate(tomllib.loads(_STUB_AGENT_DEFAULT_CONFIG_TEXT))
    else:
        try:
            config = load_config(args.config)
        except (ValueError, FileNotFoundError) as e:
            _configuration_error(str(e), code="config_load_failed", stage="config_loading")

    repository = getattr(args, "repo", None)
    if repository is not None:
        if "/" not in repository:
            owner = config.repository.owner
            if owner is None:
                _configuration_error(
                    f"--repo {repository!r} omits OWNER, but [repository].owner is not set",
                    code="invalid_repository",
                    stage="repository_setup",
                )
            repository = f"{owner}/{repository}"
        if not REPOSITORY_SLUG.fullmatch(repository):
            _configuration_error(
                f"--repo must be NAME with [repository].owner configured or an "
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
        raw_skills = (
            args.skills_dir
            if isinstance(args.skills_dir, list)
            else ([args.skills_dir] if args.skills_dir else None)
        )
        skills = resolve_skill_source_dirs(raw_skills, backend=backend)
    return config, skills, backend


def _prepare_stub_agent_smoke_defaults(argv: list[str]) -> list[str]:
    if "--stub-agent" not in argv or any(
        token == "--input" or token.startswith("--input=") for token in argv
    ):
        return argv
    return [
        "--input",
        str(_STUB_AGENT_DEFAULT_INPUT),
        "--exp-name",
        f"stub-smoke-{os.getpid()}",
        *argv,
    ]


def run_environment_spec_from_args(args: argparse.Namespace) -> RunEnvironmentSpec:
    return make_run_environment_spec(
        use_docker=args.docker,
        docker_image=args.docker_image,
        use_modal=args.modal,
        modal_gpu=args.modal_gpu,
        modal_model_volume=args.modal_model_volume,
        modal_app=args.modal_app,
    )


def _resolve_run_dir(run_dir_arg: str) -> str:
    """Resolve a run directory name.

    If *run_dir_arg* is ``"latest"``, find the most recent experiment
    directory in ``exp_env/``. Otherwise accept either a bare run directory
    name or a path whose final parent is ``exp_env``.
    """
    if run_dir_arg != "latest":
        run_dir_path = Path(run_dir_arg).expanduser()
        if run_dir_path.parent.name == "exp_env":
            project_relative = PROJECT_ROOT / run_dir_path
            if project_relative.is_dir():
                return run_dir_path.name
        if run_dir_path.is_dir():
            resolved = run_dir_path.resolve()
            if resolved.parent == (PROJECT_ROOT / "exp_env").resolve():
                return resolved.name
            return str(resolved)

        legacy_path = PROJECT_ROOT / "exp_env" / run_dir_arg
        if legacy_path.is_dir():
            return run_dir_arg

        if _is_remote_experiment(run_dir_arg):
            return _clone_experiment(run_dir_arg)
        return run_dir_arg
    exp_env = PROJECT_ROOT / "exp_env"
    if not exp_env.is_dir():
        _configuration_error(
            "exp_env/ directory does not exist.", code="resume_not_found", stage="resume_resolution"
        )
    dirs = sorted([d.name for d in exp_env.iterdir() if d.is_dir()])
    if not dirs:
        _configuration_error(
            "No experiment directories found in exp_env/.",
            code="resume_not_found",
            stage="resume_resolution",
        )
    return dirs[-1]


def _is_remote_experiment(value: str) -> bool:
    return bool(
        REPOSITORY_SLUG.fullmatch(value) or value.startswith(("https://", "ssh://", "git@"))
    )


def _clone_experiment(remote: str) -> str:
    """Clone a remote experiment into ``exp_env/`` and return its run key."""
    repository_name = remote.rstrip("/").rsplit("/", 1)[-1]
    if ":" in repository_name:
        repository_name = repository_name.rsplit(":", 1)[-1]
    if repository_name.endswith(".git"):
        repository_name = repository_name[:-4]
    if not repository_name:
        _configuration_error(
            f"Cannot determine a local directory name from --resume {remote!r}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )

    destination = PROJECT_ROOT / "exp_env" / repository_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir():
            _configuration_error(
                f"Cannot clone experiment: destination is not a directory: {destination}",
                code="resume_clone_failed",
                stage="resume_resolution",
            )
        existing_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=destination,
            capture_output=True,
            text=True,
        )
        expected_suffix = remote.removesuffix(".git").rstrip("/")
        origin = existing_origin.stdout.strip().removesuffix(".git").rstrip("/")
        if existing_origin.returncode == 0 and (
            origin == expected_suffix
            or origin.endswith(f"/{expected_suffix}")
            or origin.endswith(f":{expected_suffix}")
        ):
            return destination.name
        _configuration_error(
            f"Cannot clone {remote!r}: destination already exists with a different origin: "
            f"{destination}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    if REPOSITORY_SLUG.fullmatch(remote):
        try:
            GitHubCLI().clone_repository(remote, destination)
        except GitHubCLIError as exc:
            _configuration_error(
                f"Cannot clone experiment repository {remote!r}: {exc}",
                code="resume_clone_failed",
                stage="resume_resolution",
            )
        return destination.name

    command = ["git", "clone", remote, str(destination)]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as exc:
        tool = command[0]
        _configuration_error(
            f"Cannot clone {remote!r}: required command {tool!r} is not installed ({exc})",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        _configuration_error(
            f"Cannot clone experiment repository {remote!r}: {detail}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    return destination.name


def _resume_exp_dir(run_dir_name: str) -> Path:
    return PROJECT_ROOT / "exp_env" / run_dir_name


def _infer_resume_input(exp_dir: Path) -> Path:
    materialized_input = exp_dir / "workspace"
    if (materialized_input / "vibesys.input.toml").is_file() and (
        materialized_input / "OBJECTIVE.md"
    ).is_file():
        return materialized_input

    events_path = exp_dir / "logs" / "run-events.jsonl"
    if not events_path.is_file():
        _configuration_error(
            f"Cannot infer --input because resume metadata is missing: {events_path}",
            code="resume_input_not_found",
            stage="resume_resolution",
        )

    for line_number, line in enumerate(events_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            _configuration_error(
                f"Cannot infer --input because {events_path}:{line_number} is invalid JSON: {exc}",
                code="resume_input_invalid",
                stage="resume_resolution",
            )
        if event.get("type") != "run_started":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            break
        input_path = data.get("input")
        if isinstance(input_path, str) and input_path:
            return Path(input_path)
        break

    _configuration_error(
        f"Cannot infer --input because no run_started input was found in {events_path}",
        code="resume_input_not_found",
        stage="resume_resolution",
    )


def _resolve_resume_args(args: argparse.Namespace) -> None:
    if args.resume is None:
        return
    if args.repo is not None:
        _configuration_error(
            "--repo creates a remote for a new experiment and cannot be combined with --resume",
            code="invalid_arguments",
            stage="argument_parsing",
        )

    run_dir_name = _resolve_run_dir(args.resume)
    args.resume = run_dir_name
    if args.input is not None:
        return

    exp_dir = _resume_exp_dir(run_dir_name)
    if not exp_dir.is_dir():
        _configuration_error(
            f"Run directory does not exist: exp_env/{run_dir_name}",
            code="resume_not_found",
            stage="resume_resolution",
        )
    args.input = _infer_resume_input(exp_dir)


def _apply_common_args(parser: argparse.ArgumentParser) -> None:
    """Common args + the cross-loop ``--resume`` flag."""
    _add_common_args(parser)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Disable the interactive client even when attached to a terminal.",
    )
    parser.add_argument("--control-socket", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="RUN_DIR",
        help="Resume a previous run (default: latest).",
    )


def _make_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = _RunArgumentParser(prog=prog, description=description)
    _apply_common_args(parser)
    return parser


# ---------------------------------------------------------------------------
# Interactive setup defaults command
# ---------------------------------------------------------------------------


def _build_tui_defaults_parser() -> argparse.ArgumentParser:
    parser = _RunArgumentParser(
        prog="vibesys tui-defaults",
        description="Resolve configuration defaults for the pre-launch TUI.",
    )
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "agent.toml")
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--exp-name", default=None)
    return parser


def _run_tui_defaults(argv: list[str]) -> None:
    args = _build_tui_defaults_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (ValueError, FileNotFoundError) as exc:
        _configuration_error(str(exc), code="config_load_failed", stage="config_loading")

    input_path = args.input.expanduser().resolve() if args.input is not None else None
    experiment_name = args.exp_name or generate_experiment_name(input_path)
    defaults = InteractiveSetupDefaults(
        input_path=str(input_path) if input_path is not None else "",
        experiment_name=experiment_name,
        repository_owner=config.repository.owner,
        repository_name=repository_name_from_experiment(experiment_name),
        visibility=config.repository.visibility,
    )
    print(defaults.model_dump_json())


# ---------------------------------------------------------------------------
# Input-bundle validation command
# ---------------------------------------------------------------------------


def _build_validate_parser() -> argparse.ArgumentParser:
    parser = _RunArgumentParser(
        prog="vibesys validate",
        description="Validate an input-bundle harness contract without starting a run.",
    )
    parser.add_argument(
        "input_bundle",
        type=Path,
        nargs="?",
        default=None,
        help="Path to the input bundle (default: current directory).",
    )
    return parser


def _run_validate(argv: list[str]) -> None:
    """Validate one input-bundle contract, then report its resolved paths."""

    args = _build_validate_parser().parse_args(argv)
    input_path = (args.input_bundle or Path.cwd()).expanduser().resolve()

    try:
        bundle = load_input_bundle(input_path)
    except (FileNotFoundError, ValueError) as exc:
        _configuration_error(
            f"Validation failed for input bundle {input_path}: {exc}",
            code="validation_failed",
            stage="input_validation",
            exit_code=1,
        )

    print("VibeSys validation passed: input bundle is valid.")
    print(f"  input bundle: {bundle.root}")
    print(f"  objective: {bundle.objective_path}")
    print(f"  accuracy command: {bundle.accuracy_command_display}")
    print(f"  benchmark command: {bundle.benchmark_command_display}")
    if bundle.workspace_seed_path is not None:
        print(f"  workspace seed: {bundle.workspace_seed_path}")
    if bundle.evaluator_path is not None:
        print(f"  evaluator source: {bundle.evaluator_path}")
    if bundle.benchmark_result is not None:
        print(f"  benchmark metric: {bundle.benchmark_result.metric}")


# ---------------------------------------------------------------------------
# Shared input-bundle discovery
# ---------------------------------------------------------------------------


def _load_objective(bundle: InputBundle) -> str:
    """Return the input bundle's objective text."""
    return bundle.objective


# ===========================================================================
# agent loop  (--outer-loop agent)
# ===========================================================================


def _detect_resume_round(exp_dir: Path) -> int:
    """Infer the next round to run from ``logs/rounds.json`` (1-based)."""
    rounds_json = exp_dir / "logs" / "rounds.json"
    if not rounds_json.exists():
        return 1
    try:
        data = json.loads(rounds_json.read_text())
        return int(len(data)) + 1
    except Exception:
        return 1


def _prune_rounds_state(exp_dir: Path, keep_up_to: int) -> None:
    """Trim rounds.json to entries with round < ``keep_up_to``."""
    rounds_json = exp_dir / "logs" / "rounds.json"
    if not rounds_json.exists():
        return
    try:
        data = json.loads(rounds_json.read_text())
    except Exception:
        return
    kept = [d for d in data if int(d.get("round", 0)) < keep_up_to]
    rounds_json.write_text(json.dumps(kept, indent=2))


def _build_agent_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibesys --outer-loop agent",
        description="Run the autonomous orchestrator-driven build loop.",
    )
    parser.add_argument("--max-rounds", type=int, default=24)
    parser.add_argument("--max-retries-per-round", type=int, default=3)
    parser.add_argument(
        "--stub-agent",
        action="store_true",
        help="Use deterministic local agent responses for fast TUI smoke tests.",
    )
    parser.add_argument("--start-round", type=int, default=None, metavar="N")
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


def _validate_target_inputs(args: argparse.Namespace) -> None:
    input_arg = getattr(args, "input", None)
    if input_arg is None:
        _configuration_error(
            "Error: missing required target input: --input. "
            "Pass a bundle containing OBJECTIVE.md and vibesys.input.toml.",
            code="missing_input",
            stage="input_loading",
        )
    try:
        args.input_bundle = load_input_bundle(
            input_arg,
            allow_materialized_sources=getattr(args, "resume", None) is not None,
        )
    except (FileNotFoundError, ValueError) as exc:
        _configuration_error(str(exc), code="invalid_input", stage="input_loading")


def _validate_agent(args: argparse.Namespace) -> None:
    if args.modal and args.profiler not in _MODAL_PROFILERS:
        _configuration_error(
            "Error: --modal only supports --profiler=torch, --profiler=auto, or --profiler=none.",
        )
    if args.max_retries_per_round < 1:
        _configuration_error("Error: --max-retries-per-round must be >= 1.")
    _validate_target_inputs(args)


def _run_agent(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibesys.loops.agent.loop import run_agent_loop

    bundle: InputBundle = args.input_bundle
    objective = _load_objective(bundle)

    existing = False
    exp_name = args.exp_name
    start_round = args.start_round or 1

    if args.resume is not None:
        run_dir_name = _resolve_run_dir(args.resume)
        exp_name = run_dir_name
        existing = True
        print(f"Resuming agent run: exp_env/{run_dir_name}/")
        exp_dir = PROJECT_ROOT / "exp_env" / run_dir_name
        if not exp_dir.is_dir():
            _configuration_error(
                f"Run directory does not exist: exp_env/{run_dir_name}",
                code="resume_not_found",
                stage="resume_resolution",
            )
        from vibesys.server.registry import active_supervisor

        supervisor = active_supervisor()
        if supervisor is not None:
            supervisor.attach(exp_dir / "logs")
        if args.start_round is None:
            start_round = _detect_resume_round(exp_dir)
            print(f"Auto-detected next round: {start_round}")
        else:
            print(f"Resetting to round {start_round} (discarding later rounds).")
            _prune_rounds_state(exp_dir, keep_up_to=start_round)
        if start_round > args.max_rounds:
            _configuration_error(
                f"This run has completed {start_round - 1} rounds; --max-rounds is a total "
                f"limit. Choose --max-rounds {start_round} or greater to continue.",
                code="resume_limit_exhausted",
                stage="resume_resolution",
            )

    success = run_agent_loop(
        config=config,
        exp_name=exp_name,
        input_path=str(bundle.root),
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_seed=bundle.workspace_seed_path,
        evaluator_path=bundle.evaluator_path,
        benchmark_result=bundle.benchmark_result,
        objective=objective,
        max_rounds=args.max_rounds,
        max_retries_per_round=args.max_retries_per_round,
        start_round=start_round,
        existing=existing,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args),
        agent_backend="stub" if args.stub_agent else args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        domain=bundle.domain,
        interface=args.interface,
        inner_loop=args.inner_loop,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
    )

    if success:
        print(f"\nAgent loop completed {args.max_rounds} rounds.")
    else:
        print("\nAgent loop stopped early (exception or KeyboardInterrupt).")
        sys.exit(1)


# ===========================================================================
# evolve loop  (--outer-loop evolve)
# ===========================================================================


def _parse_cli_objective(spec: str):
    """Parse a ``--objective`` flag value (``name:direction``)."""
    from vibesys.loops.evolve.population import Objective

    if ":" not in spec:
        raise argparse.ArgumentTypeError(f"--objective {spec!r} must be 'name:max' or 'name:min'")
    name, _, direction = spec.partition(":")
    name = name.strip()
    direction = direction.strip().lower()
    if not name:
        raise argparse.ArgumentTypeError(f"--objective {spec!r}: metric name is empty")
    if direction not in ("max", "min"):
        raise argparse.ArgumentTypeError(
            f"--objective {spec!r}: direction must be 'max' or 'min', got {direction!r}"
        )
    return Objective(name=name, direction=direction)


def _load_objectives_toml(input_path: Path) -> list[Objective]:
    """Read ``objectives.toml`` from the input bundle if present."""
    from vibesys.loops.evolve.population import Objective

    path = input_path / "objectives.toml"
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    raw_list = data.get("objective") or []
    objectives = []
    for entry in raw_list:
        name = entry.get("name")
        direction = entry.get("direction")
        if not name or direction not in ("max", "min"):
            raise ValueError(
                f"Malformed entry in {path}: {entry!r}. Each [[objective]] "
                f"must set name and direction (max|min)."
            )
        objectives.append(Objective(name=name, direction=direction))
    return objectives


def _resolve_objectives(args: argparse.Namespace) -> list[Objective]:
    if args.objective:
        return list(args.objective)
    return _load_objectives_toml(args.input_bundle.root)


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
        "--objective",
        action="append",
        default=[],
        type=_parse_cli_objective,
        metavar="NAME:DIRECTION",
    )
    parser.add_argument("--frontier-bias", type=float, default=0.7)
    parser.add_argument("--modality", default="text_generation", choices=_MODALITIES)
    return parser


def _validate_evolve(args: argparse.Namespace) -> None:
    if args.modal and args.profiler not in _MODAL_PROFILERS:
        _configuration_error(
            "Error: --modal only supports --profiler=torch, --profiler=auto, or --profiler=none.",
        )
    _validate_target_inputs(args)
    if args.children_per_generation < 1:
        _configuration_error("--children-per-generation must be >= 1.")
    if args.max_generations < 1:
        _configuration_error("--max-generations must be >= 1.")
    if args.selection_temperature <= 0:
        _configuration_error("--selection-temperature must be > 0.")
    if not (0.0 <= args.frontier_bias <= 1.0):
        _configuration_error("--frontier-bias must be in [0, 1].")


def _run_evolve(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibesys.loops.evolve.loop import run_evolve_loop

    bundle: InputBundle = args.input_bundle
    objective = _load_objective(bundle)
    objectives = _resolve_objectives(args)
    if objectives:
        spec = ", ".join(f"{o.name}({o.direction})" for o in objectives)
        print(f"Pareto mode active: [{spec}]; frontier_bias={args.frontier_bias}")

    existing = False
    exp_name = args.exp_name
    if args.resume is not None:
        run_dir_name = _resolve_run_dir(args.resume)
        exp_name = run_dir_name
        existing = True
        print(f"Resuming evolve run: exp_env/{run_dir_name}/")

    success = run_evolve_loop(
        config=config,
        exp_name=exp_name,
        input_path=str(bundle.root),
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_seed=bundle.workspace_seed_path,
        evaluator_path=bundle.evaluator_path,
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
        run_environment=run_environment_spec_from_args(args),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        objectives=objectives,
        frontier_bias=args.frontier_bias,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
    )

    if success:
        print(
            f"\nEvolve loop completed {args.max_generations} generations "
            f"× {args.children_per_generation} cands."
        )
    else:
        print("\nEvolve loop stopped early (exception or KeyboardInterrupt).")
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
    parser.add_argument("--start-round", type=int, default=None, metavar="N")
    return parser


def _validate_plain(args: argparse.Namespace) -> None:
    if args.modal and args.profiler not in _MODAL_PROFILERS:
        _configuration_error(
            "Error: --modal only supports --profiler=torch, --profiler=auto, or --profiler=none.",
        )
    _validate_target_inputs(args)


def _run_plain(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibesys.loops.plain.loop import (
        PlainLoopState,
        load_state,
        run_plain_loop,
    )

    bundle: InputBundle = args.input_bundle

    existing = False
    exp_name = args.exp_name
    resume_state: PlainLoopState | None = None

    if args.resume is not None:
        run_dir_name = _resolve_run_dir(args.resume)
        exp_name = run_dir_name
        existing = True
        print(f"Resuming from: exp_env/{run_dir_name}/")

        exp_dir = PROJECT_ROOT / "exp_env" / run_dir_name
        log_dir = exp_dir / "logs"

        if args.start_round is not None:
            resume_state = PlainLoopState(
                round_idx=args.start_round - 1,
                bootstrap_done=True,
            )
        else:
            resume_state = load_state(log_dir)
            if resume_state is not None:
                print(
                    f"Auto-detected state: round {resume_state.round_idx + 1}, "
                    f"phase '{resume_state.phase}'"
                    + (
                        f", current issue #{resume_state.current_issue_id}"
                        if resume_state.current_issue_id
                        else ""
                    )
                )
            else:
                resume_state = PlainLoopState(bootstrap_done=True)
                print(
                    "Warning: state.json not found. Starting fresh "
                    "(bootstrap will be skipped because existing run)."
                )

    success = run_plain_loop(
        config=config,
        exp_name=exp_name,
        input_path=str(bundle.root),
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        workspace_seed=bundle.workspace_seed_path,
        evaluator_path=bundle.evaluator_path,
        max_rounds=args.max_rounds,
        max_attempts_per_issue=args.max_attempts_per_issue,
        max_issues_per_perf_eval=args.max_issues_per_perf_eval,
        existing=existing,
        resume_state=resume_state,
        debug=args.debug,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        remote_repo=args.repo,
        repo_visibility=args.repo_visibility,
    )

    if success:
        print("\nPlain loop completed: no remaining open issues.")
    else:
        print(f"\nPlain loop did not complete after {args.max_rounds} rounds.")
        sys.exit(1)


# ===========================================================================
# Dispatch
# ===========================================================================


@dataclass(frozen=True)
class _LoopCommand:
    """Typed dispatch record for one ``--outer-loop`` kind."""

    build_parser: Callable[[], argparse.ArgumentParser]
    validate: Callable[[argparse.Namespace], None]
    run: Callable[[argparse.Namespace], None]


_LOOP_COMMANDS: dict[str, _LoopCommand] = {
    "agent": _LoopCommand(_build_agent_parser, _validate_agent, _run_agent),
    "plain": _LoopCommand(_build_plain_parser, _validate_plain, _run_plain),
    "evolve": _LoopCommand(_build_evolve_parser, _validate_evolve, _run_evolve),
}


def parse_cli_invocation(argv: list[str]) -> CliInvocation:
    """Parse and validate one invocation without printing or exiting."""
    argv = _prepare_stub_agent_smoke_defaults(argv)
    loop_kind, remaining = _extract_loop_selection(argv)
    command = _LOOP_COMMANDS[loop_kind]
    args = command.build_parser().parse_args(remaining)
    _resolve_resume_args(args)
    command.validate(args)
    return CliInvocation(loop_kind=loop_kind, args=args)


def _dispatch(argv: list[str]) -> None:
    if argv and argv[0] == "tui-defaults":
        _run_tui_defaults(argv[1:])
        return
    if argv and argv[0] == "validate":
        _run_validate(argv[1:])
        return

    invocation = parse_cli_invocation(argv)
    loop_kind, args = invocation.loop_kind, invocation.args
    runner = _LOOP_COMMANDS[loop_kind].run
    from vibesys.server.events import EventStatus, EventType, RunStartedData
    from vibesys.server.registry import active_supervisor

    supervisor = active_supervisor()
    if supervisor is not None:
        max_rounds = getattr(args, "max_rounds", getattr(args, "max_iterations", 1))
        supervisor.record(
            EventType.RUN_STARTED,
            status=EventStatus.ACTIVE,
            data=RunStartedData(
                outer_loop=loop_kind,
                input=str(args.input_bundle.root),
                max_rounds=max_rounds,
            ),
        )
    runner(args)


def _control_socket_from_argv(argv: list[str]) -> Path | None:
    """Read the transport bootstrap flag without parsing run configuration."""
    for index, token in enumerate(argv):
        if token.startswith("--control-socket="):
            value = token.partition("=")[2]
            return Path(value) if value else None
        if token == "--control-socket" and index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


def _render_configuration_error(error: ConfigurationError) -> NoReturn:
    diagnostic = error.diagnostic
    print(f"vibesys: {diagnostic.message}", file=sys.stderr)
    if diagnostic.usage:
        print(diagnostic.usage, file=sys.stderr)
    raise SystemExit(diagnostic.exit_code)


def main() -> None:
    argv = sys.argv[1:]
    control_socket = _control_socket_from_argv(argv)
    if control_socket is not None:
        from vibesys.server.runtime import run_server

        try:
            run_server(lambda: _dispatch(argv), socket_path=control_socket)
        except ConfigurationError as exc:
            raise SystemExit(exc.diagnostic.exit_code) from None
        return

    try:
        _dispatch(argv)
    except ConfigurationError as exc:
        _render_configuration_error(exc)


if __name__ == "__main__":
    main()
