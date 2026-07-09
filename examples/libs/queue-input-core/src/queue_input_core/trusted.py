from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from queue_input_core.config import QueueInputConfig, load_config
from queue_input_core.contract import QueueContract, get_contract

TRUSTED_SCENARIOS = ("spsc", "mpsc", "mpmc")
_HARNESS_DIR = Path(__file__).with_name("trusted_harness")


def _worker_defaults(contract: QueueContract | None, config: QueueInputConfig) -> tuple[int, int]:
    producers = (
        config.producers
        if config.producers is not None
        else (contract.default_producers if contract else 4)
    )
    consumers = (
        config.consumers
        if config.consumers is not None
        else (contract.default_consumers if contract else 4)
    )
    return producers, consumers


def _capacity_default(contract: QueueContract | None, config: QueueInputConfig) -> int:
    return (
        config.capacity
        if config.capacity is not None
        else (contract.default_capacity if contract else 1024)
    )


def _run_harness(arguments: list[str], workspace: Path) -> int:
    command = [
        "go",
        "run",
        ".",
        *arguments,
        "--workspace",
        str(workspace),
        "--candidate",
        "queue-candidate",
    ]
    try:
        return subprocess.run(command, cwd=_HARNESS_DIR, check=False).returncode
    except FileNotFoundError:
        print("FAIL - Go is required to run the trusted queue harness", file=sys.stderr)
        return 1


def _selected_scenarios(fixed_scenario: str | None, selected: str) -> tuple[str, ...]:
    if fixed_scenario is not None:
        return (fixed_scenario,)
    if selected == "all":
        return TRUSTED_SCENARIOS
    return (selected,)


def accuracy_main(fixed_scenario: str | None = None) -> None:
    input_config = load_config()
    contract = get_contract(fixed_scenario) if fixed_scenario else None
    producers, consumers = _worker_defaults(contract, input_config)
    parser = argparse.ArgumentParser(
        description="Trusted linearizability checker for bounded queues."
    )
    if fixed_scenario is None:
        parser.add_argument(
            "--scenario",
            choices=[*TRUSTED_SCENARIOS, "all"],
            default=input_config.scenario or "all",
        )
    parser.add_argument("--capacity", type=int, default=_capacity_default(contract, input_config))
    parser.add_argument("--ops", type=int, default=24)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--producers", type=int, default=producers)
    parser.add_argument("--consumers", type=int, default=consumers)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--failure-history", type=Path, default=None)
    parser.add_argument("--use-reference", action="store_true")
    args = parser.parse_args()

    selected = fixed_scenario or args.scenario
    workspace = Path.cwd().resolve()
    ok = True
    for scenario in _selected_scenarios(fixed_scenario, selected):
        arguments = [
            "check",
            "--scenario",
            scenario,
            "--capacity",
            str(args.capacity),
            "--operations",
            str(args.ops),
            "--trials",
            str(args.trials),
            "--producers",
            str(args.producers),
            "--consumers",
            str(args.consumers),
            "--seed",
            str(args.seed),
        ]
        if args.use_reference:
            arguments.append("--use-reference")
        if args.failure_history:
            failure_path = args.failure_history
            if selected == "all":
                failure_path = failure_path.with_name(
                    f"{failure_path.stem}-{scenario}{failure_path.suffix}"
                )
            arguments.extend(["--failure-history", str(failure_path.resolve())])
        ok = _run_harness(arguments, workspace) == 0 and ok
    raise SystemExit(0 if ok else 1)


def benchmark_main(fixed_scenario: str | None = None) -> None:
    input_config = load_config()
    contract = get_contract(fixed_scenario) if fixed_scenario else None
    producers, consumers = _worker_defaults(contract, input_config)
    parser = argparse.ArgumentParser(description="Trusted throughput benchmark for bounded queues.")
    if fixed_scenario is None:
        parser.add_argument(
            "--scenario",
            choices=[*TRUSTED_SCENARIOS, "all"],
            default=input_config.scenario or "spsc",
        )
    parser.add_argument("--capacity", type=int, default=_capacity_default(contract, input_config))
    parser.add_argument("--producers", type=int, default=producers)
    parser.add_argument("--consumers", type=int, default=consumers)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--use-reference", action="store_true")
    args = parser.parse_args()

    selected = fixed_scenario or args.scenario
    workspace = Path.cwd().resolve()
    results: list[dict] = []
    ok = True
    for scenario in _selected_scenarios(fixed_scenario, selected):
        with tempfile.NamedTemporaryFile(suffix=".json") as output:
            arguments = [
                "benchmark",
                "--scenario",
                scenario,
                "--capacity",
                str(args.capacity),
                "--producers",
                str(args.producers),
                "--consumers",
                str(args.consumers),
                "--duration",
                f"{args.duration}s",
                "--warmup",
                f"{args.warmup}s",
                "--seed",
                str(args.seed),
                "--output-json",
                output.name,
            ]
            if args.use_reference:
                arguments.append("--use-reference")
            returncode = _run_harness(arguments, workspace)
            ok = returncode == 0 and ok
            if returncode == 0:
                results.extend(json.loads(Path(output.name).read_text()))

    if args.output_json and ok:
        args.output_json.write_text(json.dumps(results, indent=2))
        print(f"Results written to {args.output_json}")
    raise SystemExit(0 if ok else 1)
