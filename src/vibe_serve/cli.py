"""Unified ``vibe-serve`` CLI.

The loop is picked by ``--outer-loop {agent, plain, evolve}``:

  "agent"  — an LLM Orchestrator decides per-round what to build next.
             Its issue board lives in the workspace as roadmap.md +
             progress.md, owned by the orchestrator.
  "plain"  — deterministic outer loop. Its issue board is a structured
             :class:`IssueBoard` (issues.json) that perf_eval files into
             and the implementer drains one issue at a time.
  "evolve" — population-based evolutionary search.

Each kind has its own ``_build_<kind>_parser`` and ``_run_<kind>``
function, sharing common args via :func:`_apply_common_args`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from vibe_serve.config import Config, _load_config
from vibe_serve.constants import (
    ComputeBackend,
    KNOWN_COMPUTE_BACKENDS,
    PROJECT_ROOT,
)
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)

_OUTER_LOOPS = ("agent", "plain", "evolve")
_MODALITIES = (
    "text_generation",
    "image_generation",
    "video_generation",
    "text_to_speech",
    "speech_to_text",
    "realtime_audio",
)


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
            value = tok[len(eq_form):]
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
    print(
        f"vibe-serve: {msg}\n"
        f"Usage: vibe-serve --outer-loop {{{'|'.join(_OUTER_LOOPS)}}} "
        f"[loop-specific args...]",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Common parser scaffolding
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add CLI arguments shared across every outer-loop parser."""
    parser.add_argument(
        "--ref",
        default="examples/Llama-3-8B/reference",
        help="Path to reference implementation file, or directory containing at least one reference.py (default: examples/Llama-3-8B/reference)",
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
        "--acc-checker",
        type=Path,
        default=Path("examples/Llama-3-8B/accuracy_checker"),
        help="Path to a directory containing accuracy checker code/documents (default: examples/Llama-3-8B/accuracy_checker).",
    )
    parser.add_argument(
        "--bench",
        type=Path,
        default=Path("examples/Llama-3-8B/benchmark"),
        help="Path to a directory containing benchmark code/documents (default: examples/Llama-3-8B/benchmark).",
    )
    parser.add_argument(
        "--nsys-profiler",
        type=Path,
        default=None,
        help="Path to directory containing nsys analysis script (analyze_nsys.py). Enables nsys profiling between agent rounds.",
    )
    parser.add_argument(
        "--torch-profiler",
        type=Path,
        default=None,
        help="Path to directory containing torch.profiler analysis script (analyze_torch_profile.py). Used when --profiler=torch.",
    )
    parser.add_argument(
        "--profiler",
        choices=["nsys", "torch", "auto"],
        default="auto",
        help=(
            "Which profiler to use between rounds. "
            "'nsys' for NVIDIA Nsight Systems (needs /proc/driver/nvidia), "
            "'torch' for torch.profiler (works in Modal sandboxes), "
            "'auto' picks torch when --modal is set, else nsys. Default: auto."
        ),
    )
    parser.add_argument(
        "--skills-dir",
        default=[Path("resources/skills/serving-systems")],
        action="append",
        type=Path,
        help=(
            "Path to a skill source (can be repeated). Each entry can be "
            "either a single skill directory (containing a top-level "
            "`SKILL.md`) or a parent directory of multiple skill directories. "
            "Default: `resources/skills/serving-systems/`."
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
        default="vibeserve",
        help="Default Modal App name (suggested to the implementer). Default: vibeserve.",
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
    try:
        config = _load_config(args.config)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    skills = (
        [str(s) for s in args.skills_dir]
        if isinstance(args.skills_dir, list)
        else ([str(args.skills_dir)] if args.skills_dir else None)
    )
    backend: ComputeBackend = args.backend or config.backend.name
    return config, skills, backend


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
    directory in ``exp_env/``. Otherwise return *run_dir_arg* as-is.
    """
    if run_dir_arg != "latest":
        return run_dir_arg
    exp_env = PROJECT_ROOT / "exp_env"
    if not exp_env.is_dir():
        print("Error: exp_env/ directory does not exist.", file=sys.stderr)
        sys.exit(1)
    dirs = sorted([d.name for d in exp_env.iterdir() if d.is_dir()])
    if not dirs:
        print("Error: no experiment directories found in exp_env/.", file=sys.stderr)
        sys.exit(1)
    return dirs[-1]


def _apply_common_args(parser: argparse.ArgumentParser) -> None:
    """Common args + the cross-loop ``--resume`` flag."""
    _add_common_args(parser)
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="RUN_DIR",
        help="Resume a previous run (default: latest).",
    )


def _make_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    _apply_common_args(parser)
    return parser


# ---------------------------------------------------------------------------
# Shared OBJECTIVE.md discovery (agent + evolve)
# ---------------------------------------------------------------------------


def _load_objective(reference_path: str) -> str:
    """Return the contents of OBJECTIVE.md sibling to *reference_path*."""
    ref = Path(reference_path).expanduser().resolve()
    objective_md = ref.parent / "OBJECTIVE.md"
    if not objective_md.exists():
        raise FileNotFoundError(
            f"OBJECTIVE.md not found at {objective_md}. Create one next to the "
            f"model input directory; it should describe the objective "
            f"(metric, goal, and any model-specific notes)."
        )
    return objective_md.read_text()


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
        prog="vibe-serve --outer-loop agent",
        description="Run the autonomous orchestrator-driven build loop.",
    )
    parser.add_argument("--max-rounds", type=int, default=24)
    parser.add_argument("--max-retries-per-round", type=int, default=3)
    parser.add_argument("--start-round", type=int, default=None, metavar="N")
    parser.add_argument(
        "--modality", default="text_generation", choices=_MODALITIES
    )
    return parser


def _validate_agent(args: argparse.Namespace) -> None:
    if args.modal and args.profiler == "nsys":
        print("Error: --modal only supports --profiler=torch.", file=sys.stderr)
        sys.exit(2)


def _run_agent(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibe_serve.loops.agent.loop import run_agent_loop

    objective = _load_objective(args.ref)

    existing = False
    exp_name = args.exp_name
    start_round = args.start_round or 1

    if args.resume is not None:
        run_dir_name = _resolve_run_dir(args.resume)
        exp_name = run_dir_name
        existing = True
        print(f"Resuming agent run: exp_env/{run_dir_name}/")
        exp_dir = PROJECT_ROOT / "exp_env" / run_dir_name
        if args.start_round is None:
            start_round = _detect_resume_round(exp_dir)
            print(f"Auto-detected next round: {start_round}")
        else:
            print(f"Resetting to round {start_round} (discarding later rounds).")
            _prune_rounds_state(exp_dir, keep_up_to=start_round)

    success = run_agent_loop(
        config=config,
        exp_name=exp_name,
        reference_path=args.ref,
        objective=objective,
        max_rounds=args.max_rounds,
        max_retries_per_round=args.max_retries_per_round,
        start_round=start_round,
        existing=existing,
        debug=args.debug,
        acc_checker=str(args.acc_checker) if args.acc_checker else None,
        bench=str(args.bench) if args.bench else None,
        nsys_profiler=str(args.nsys_profiler) if args.nsys_profiler else None,
        torch_profiler=str(args.torch_profiler) if args.torch_profiler else None,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
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
    from vibe_serve.loops.evolve.population import Objective

    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"--objective {spec!r} must be 'name:max' or 'name:min'"
        )
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


def _load_objectives_toml(reference_path: str) -> list:
    """Read ``objectives.toml`` sibling to OBJECTIVE.md if present."""
    from vibe_serve.loops.evolve.population import Objective

    ref = Path(reference_path).expanduser().resolve()
    path = ref.parent / "objectives.toml"
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


def _resolve_objectives(args: argparse.Namespace) -> list:
    if args.objective:
        return list(args.objective)
    return _load_objectives_toml(args.ref)


def _build_evolve_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibe-serve --outer-loop evolve",
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
    parser.add_argument(
        "--modality", default="text_generation", choices=_MODALITIES
    )
    return parser


def _validate_evolve(args: argparse.Namespace) -> None:
    if args.modal and args.profiler == "nsys":
        print("Error: --modal only supports --profiler=torch.", file=sys.stderr)
        sys.exit(2)
    if args.children_per_generation < 1:
        print("Error: --children-per-generation must be >= 1.", file=sys.stderr)
        sys.exit(2)
    if args.max_generations < 1:
        print("Error: --max-generations must be >= 1.", file=sys.stderr)
        sys.exit(2)
    if args.selection_temperature <= 0:
        print("Error: --selection-temperature must be > 0.", file=sys.stderr)
        sys.exit(2)
    if not (0.0 <= args.frontier_bias <= 1.0):
        print("Error: --frontier-bias must be in [0, 1].", file=sys.stderr)
        sys.exit(2)


def _run_evolve(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibe_serve.loops.evolve.loop import run_evolve_loop

    objective = _load_objective(args.ref)
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
        reference_path=args.ref,
        objective=objective,
        max_generations=args.max_generations,
        children_per_generation=args.children_per_generation,
        k_top_inspirations=args.k_top_inspirations,
        k_random_inspirations=args.k_random_inspirations,
        selection_temperature=args.selection_temperature,
        seed=args.seed,
        existing=existing,
        debug=args.debug,
        acc_checker=str(args.acc_checker) if args.acc_checker else None,
        bench=str(args.bench) if args.bench else None,
        nsys_profiler=str(args.nsys_profiler) if args.nsys_profiler else None,
        torch_profiler=str(args.torch_profiler) if args.torch_profiler else None,
        profiler_kind=args.profiler,
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
        modality=args.modality,
        objectives=objectives,
        frontier_bias=args.frontier_bias,
    )

    if success:
        print(
            f"\nEvolve loop completed {args.max_generations} generations "
            f"× {args.children_per_generation} children."
        )
    else:
        print("\nEvolve loop stopped early (exception or KeyboardInterrupt).")
        sys.exit(1)


# ===========================================================================
# plain loop  (--outer-loop plain)
# ===========================================================================


def _build_plain_parser() -> argparse.ArgumentParser:
    parser = _make_parser(
        prog="vibe-serve --outer-loop plain",
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
    if args.modal and args.profiler == "nsys":
        print("Error: --modal only supports --profiler=torch.", file=sys.stderr)
        sys.exit(2)


def _run_plain(args: argparse.Namespace) -> None:
    config, skills, backend = load_config_and_skills(args)
    from vibe_serve.loops.plain.loop import (
        PlainLoopState,
        _load_state,
        run_plain_loop,
    )

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
                round_idx=args.start_round - 1, bootstrap_done=True,
            )
        else:
            resume_state = _load_state(log_dir)
            if resume_state is not None:
                print(
                    f"Auto-detected state: iteration {resume_state.round_idx + 1}, "
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
        reference_path=args.ref,
        max_rounds=args.max_rounds,
        max_attempts_per_issue=args.max_attempts_per_issue,
        max_issues_per_perf_eval=args.max_issues_per_perf_eval,
        existing=existing,
        resume_state=resume_state,
        debug=args.debug,
        acc_checker=str(args.acc_checker) if args.acc_checker else None,
        bench=str(args.bench) if args.bench else None,
        nsys_profiler=str(PROJECT_ROOT / "examples" / "nsys_profiler"),
        skills_dirs=skills,
        run_environment=run_environment_spec_from_args(args),
        agent_backend=args.agent_backend,
        cli_provider=args.cli_provider,
        backend=backend,
    )

    if success:
        print("\nPlain loop completed: no remaining open issues.")
    else:
        print(f"\nPlain loop did not complete after {args.max_rounds} rounds.")
        sys.exit(1)


# ===========================================================================
# Dispatch
# ===========================================================================


_PARSER_BUILDERS = {
    "agent": _build_agent_parser,
    "plain": _build_plain_parser,
    "evolve": _build_evolve_parser,
}

_VALIDATORS = {
    "agent": "_validate_agent",
    "plain": "_validate_plain",
    "evolve": "_validate_evolve",
}

_RUNNERS = {
    "agent": "_run_agent",
    "plain": "_run_plain",
    "evolve": "_run_evolve",
}


def main() -> None:
    loop_kind, remaining = _extract_loop_selection(sys.argv[1:])
    args = _PARSER_BUILDERS[loop_kind]().parse_args(remaining)
    # Resolve validator + runner via globals() so unittest.mock.patch on
    # ``vibe_serve.cli._{validate,run}_<kind>`` takes effect.
    globals()[_VALIDATORS[loop_kind]](args)
    globals()[_RUNNERS[loop_kind]](args)


if __name__ == "__main__":
    main()
