"""CLI benchmark and VibeSys result-protocol-v2 adapter.

Public interface: ``main(argv=None)`` and the documented command line. The
evaluator owns all measurements; this adapter publishes one result or error.
Importing it or requesting help does not import MLX or load a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4


def _record(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, allow_nan=False) + "\n")
    stream.flush()


def main(argv: list[str] | None = None, *, runner: Callable[..., Any] | None = None) -> int:
    """Measure four correct concurrent streams after the required warmup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir", type=Path, default=Path("artifacts") / f"benchmark-{uuid4().hex}"
    )
    parser.add_argument("--seed", type=int, help="Replay a saved workload seed.")
    parser.add_argument("--model-path", type=Path, help="Existing cached model snapshot.")
    parser.add_argument("--vs-output", type=Path, help="Exclusive protocol-v2 JSONL destination.")
    parser.add_argument("--round", type=int, help="Actual VibeSys round; use 0 for baseline.")
    parser.add_argument(
        "--status",
        choices=["baseline", "official", "provisional", "unassigned"],
        default="unassigned",
        help="Unassigned measurements require report-time round association.",
    )
    args = parser.parse_args(argv)
    if args.round is not None and args.round < 0:
        parser.error("--round must be nonnegative")
    if args.status == "baseline" and args.round != 0:
        parser.error("--status baseline requires --round 0")
    if args.round == 0 and args.status != "baseline":
        parser.error("--round 0 requires --status baseline")
    if args.status in {"official", "provisional"} and (args.round is None or args.round < 1):
        parser.error("official/provisional status requires a positive --round")
    if args.artifact_dir.exists():
        parser.error(f"Artifact directory already exists: {args.artifact_dir}")
    if args.vs_output is not None and args.vs_output.resolve().is_relative_to(
        args.artifact_dir.resolve()
    ):
        parser.error(
            "--vs-output must be outside --artifact-dir to preserve exclusive artifact creation"
        )
    if args.vs_output is not None and args.vs_output.exists():
        parser.error(f"Protocol output already exists: {args.vs_output}")
    if args.vs_output is not None:
        args.vs_output.parent.mkdir(parents=True, exist_ok=True)
    stream = args.vs_output.open("x", encoding="utf-8") if args.vs_output else sys.stdout
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from evaluator import METRIC_SPECS

        _record(stream, {"kind": "hello", "protocol": 2, "metrics": METRIC_SPECS})
        return _measure(args, stream, runner)
    finally:
        if args.vs_output:
            stream.close()


def _measure(args: argparse.Namespace, stream: TextIO, runner: Callable[..., Any] | None) -> int:
    try:
        from evaluator import SavedMeasurement, run_evaluation

        result = (runner or run_evaluation)(
            "benchmark", args.artifact_dir, args.seed, model_path=args.model_path
        )
        measurement = SavedMeasurement(
            schema_version=1,
            round=args.round,
            status=args.status,
            metrics=result.metrics,
            diagnostics=result.diagnostics,
            artifacts=result.artifacts,
        )
        with (args.artifact_dir / "measurement.json").open("x", encoding="utf-8") as destination:
            json.dump(measurement.model_dump(), destination, indent=2, allow_nan=False)
            destination.write("\n")
    except Exception as exc:
        _record(stream, {"kind": "error", "message": str(exc) or type(exc).__name__})
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    _record(stream, {"kind": "result", "values": result.metrics})
    print(f"Benchmark artifacts: {args.artifact_dir.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
