"""Argparse construction: parser builders and their small parse/type helpers."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from entrypoints.cli.constants import _MIGRATE_RUN_ENVIRONMENT_COMMAND, _MODALITIES, _OUTER_LOOPS
from entrypoints.cli.errors import _configuration_error, _RunArgumentParser
from vibesys.api import (
    KNOWN_COMPUTE_BACKENDS,
    ComputeBackend,
    ConfigurationDiagnostic,
    ConfigurationError,
    DomainName,
    Objective,
    ProfilerKind,
    RepositoryVisibility,
)
from vibesys.api.request import CLI_PROFILER_CHOICES, coerce_profiler_kind
from vs_agent.api import SHIPPED_PROVIDERS


def _parse_profiler_kind(value: str) -> ProfilerKind:
    try:
        return coerce_profiler_kind(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_runs_dir(value: str) -> Path:
    if not value.strip():
        message = "must not be empty"
        raise argparse.ArgumentTypeError(message)
    return Path(value)


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
            "'rocprof' for AMD rocprofv3 / rocprof-compute (ROCm system trace, "
            "PMC counters, ATT, kernel-altitude counters), "
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
        choices=["cli"],
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


def _parse_cli_objective(spec: str) -> Objective:
    """Parse a ``--objective`` flag value (``name:direction``)."""
    if ":" not in spec:
        message = f"--objective {spec!r} must be 'name:max' or 'name:min'"
        raise argparse.ArgumentTypeError(message)
    name, _, direction = spec.partition(":")
    name = name.strip()
    direction = direction.strip().lower()
    if not name:
        _exception_message = f"--objective {spec!r}: metric name is empty"
        raise argparse.ArgumentTypeError(_exception_message)
    if direction not in ("max", "min"):
        _exception_message_2 = (
            f"--objective {spec!r}: direction must be 'max' or 'min', got {direction!r}"
        )
        raise argparse.ArgumentTypeError(_exception_message_2)
    return Objective(name=name, direction=direction)


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
