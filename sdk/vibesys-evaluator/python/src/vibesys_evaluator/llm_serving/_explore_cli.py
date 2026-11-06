"""The command line of ``agentx.explore``, for a shell in a vibesys-cli lab.

    python -m vibesys_evaluator.llm_serving.agentx explore --model M --tokenizer T \
        --tokenizer-revision R --max-context-length N --concurrency 8 --duration-secs 120 \
        --artifacts /vibesys/artifacts/agentx [--warmup-requests-per-lane 1] \
        [--dataset-entries 4] [--seed 42] [--url URL] [--timeout-secs 3300]

It prints the metrics as one JSON object on standard output and exits 0, exits
1 naming what failed on standard error when the replay does (the harness's
own failures included), and 2 for a usage error. The task's
own benchmark script names the model, tokenizer, revision and context length
it scores with; pass the same ones to explore the same server.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys_evaluator._private.sample import SampleError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Exit status of a replay that failed, and of a usage error (argparse's).
FAILED, USAGE = 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vibesys_evaluator.llm_serving.agentx",
        description="Replay AgentX with any load; never a measurement.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    explore = commands.add_parser("explore", help="replay with the load given")
    for flag in ("--model", "--tokenizer", "--tokenizer-revision"):
        explore.add_argument(flag, required=True)
    for flag in ("--concurrency", "--max-context-length", "--duration-secs"):
        explore.add_argument(flag, type=int, required=True)
    explore.add_argument("--artifacts", type=Path, required=True)
    explore.add_argument("--warmup-requests-per-lane", type=int, default=1)
    explore.add_argument("--dataset-entries", type=int, default=None)
    explore.add_argument("--seed", type=int, default=42)
    explore.add_argument("--url", default=None, help="the server's base URL (default: VIBESYS_SERVICE_URL)")
    explore.add_argument("--timeout-secs", type=float, default=3300.0)
    return parser


def main(argv: Sequence[str], *, explore: Callable[..., dict[str, float]]) -> int:
    """Run the command line over ``explore`` and return the exit status."""
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as stop:
        return int(stop.code) if isinstance(stop.code, int) else USAGE
    try:
        values = explore(
            model=args.model,
            tokenizer=args.tokenizer,
            tokenizer_revision=args.tokenizer_revision,
            concurrency=args.concurrency,
            max_context_length=args.max_context_length,
            artifacts=args.artifacts,
            duration_secs=args.duration_secs,
            warmup_requests_per_lane=args.warmup_requests_per_lane,
            dataset_entries=args.dataset_entries,
            seed=args.seed,
            url=args.url,
            timeout_secs=args.timeout_secs,
        )
    except ValueError as error:
        print(f"agentx explore: {error}", file=sys.stderr)
        return USAGE
    except SampleError as error:
        print(f"agentx explore: {error}", file=sys.stderr)
        return FAILED
    print(json.dumps(values, sort_keys=True))
    return 0
