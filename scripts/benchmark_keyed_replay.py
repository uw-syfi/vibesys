"""Manual performance workload, separate from deterministic correctness tests.

Run: uv run python -m scripts.benchmark_keyed_replay
The default builds 10,000 operations across 100 branches, alternating proposal
and selector-admission observations. The root folds every report into its causal
frontier; every admission builds and classifies a typed prompt context. Timings
are reported, never used as correctness assertions or pytest verdicts.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import sys
import tracemalloc
from importlib import import_module
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.any_pb import Any

from vs_core.api.replay import (
    AcceptedReplay,
    ReplayCall,
    ReplayFlow,
    ReplayScheduler,
    ReplaySettings,
    SemanticKey,
    classify_context,
)
from vs_proto.api import core_common, replay
from vs_sim.api import MonotonicClock

Context = import_module("libs.vs-core.tests.generated.vibesys.replay.testing.v1.context_pb").Context

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

Reply = core_common.Identity


def _call(key: SemanticKey, candidate: str) -> ReplayCall[Reply]:
    context = Context(
        candidate=candidate, wording="Propose or admit this candidate", evidence=[candidate]
    )
    return ReplayCall(
        key,
        classify_context(context, contract="benchmark.v1"),
        "benchmark.v1",
        replay.ReplayPolicy(
            role="planner",
            template="benchmark.j2",
            content=context.to_binary(),
            reply_type=Reply.desc().type_name,
        ),
        Reply,
    )


def workload(
    branches: int, rounds: int
) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
    """Build a long causal fold with fan-out and shared-fact selector admissions."""

    async def root(ctx: ReplayFlow) -> object:
        channel = ctx.channel()
        group = ctx.group()

        def worker(branch: int) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
            async def child(child_ctx: ReplayFlow) -> object:
                for sample in range(rounds):
                    key = SemanticKey("proposal").item(str(branch)).sample(str(sample))
                    result = await child_ctx.call(_call(key, f"candidate-{branch}-{sample}"))
                    channel.send(result.root)
                return None

            return child

        for branch in range(branches):
            group.start(worker(branch))
        admissions = 0
        for _ in range(branches * rounds):
            candidate = await channel
            await ctx.call(_call(SemanticKey("admit").item(candidate), candidate))
            admissions += 1
        await group.all()
        return admissions

    return root


def build_history(
    root: Callable[[ReplayFlow], Coroutine[object, object, object]], settings: ReplaySettings
) -> tuple[AcceptedReplay, ...]:
    """Produce a real live history through the same public scheduler API."""
    live = ReplayScheduler(root, (), settings)
    history = []
    while not live.completed:
        starts = live.dispatches()
        # Admit promptly between fan-out deliveries to stress the root's causal chain.
        start = next(
            (item for item in starts if item.key is not None and item.key.operation == "admit"),
            starts[0],
        )
        sequence = len(history) + 1
        start.outcome = Any(
            type_url="type.googleapis.com/" + Reply.desc().type_name,
            value=Reply(root=f"result-{sequence}").to_binary(),
        )
        start.accepted_sequence = sequence
        start.paid = True
        observation = AcceptedReplay(sequence, float(sequence), start.to_binary())
        history.append(observation)
        live.accept(observation)
    return tuple(history)


def fresh_history(history: tuple[AcceptedReplay, ...]) -> tuple[AcceptedReplay, ...]:
    """Discard live-run memoization so the benchmark includes a cold replay index."""
    return tuple(
        AcceptedReplay(item.sequence, item.clock, item.operation_bytes) for item in history
    )


def measure(branches: int, rounds: int) -> dict[str, object]:
    """Measure replay separately from history generation, and memory separately from time."""
    allowance = replay.ReplayAllowance(
        iterations=branches * rounds, capacity=branches, remaining=1
    ).to_binary()
    settings = ReplaySettings(0, allowance, allowance)
    root = workload(branches, rounds)
    history = build_history(root, settings)
    gc.collect()
    clock = MonotonicClock()
    began = clock.now()
    run = ReplayScheduler(root, fresh_history(history), settings)
    elapsed = clock.now() - began
    result = run.result
    del run
    gc.collect()
    tracemalloc.start()
    run = ReplayScheduler(root, fresh_history(history), settings)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if run.result != result or run.dispatches() or run.frontier().unused:
        problem = "benchmark reconstruction diverged"
        raise ValueError(problem)
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "operations": len(history),
        "branches": branches,
        "replay_seconds": elapsed,
        "incremental_peak_mib": peak / (1024 * 1024),
        "admissions": result,
    }


def main() -> None:
    """Print reproducible measurements without introducing a timing-based unit test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branches", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=50)
    args = parser.parse_args()
    sys.stdout.write(json.dumps(measure(args.branches, args.rounds), indent=2) + "\n")


if __name__ == "__main__":
    main()
