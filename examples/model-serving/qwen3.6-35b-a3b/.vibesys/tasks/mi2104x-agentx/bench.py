"""Qwen3.6-35B-A3B AgentX replay throughput and latency, against the candidate's server.

benchmark   the scored AgentX replay (full mode); vibesys-cli appends --vs-output FILE
profile     a short smoke replay (two corpus entries, 60 s): the load to record under a
            profiler, or a quick try of the chat path; never scored

The accuracy check is checker.py, run on its own (vibesys.input.toml, [accuracy]).
The recipe is what the earlier `agentx-v1` evaluator ran with these arguments:
--mode full --model Qwen/Qwen3.6-35B-A3B --tokenizer Qwen/Qwen3.6-35B-A3B
--tokenizer-revision 995ad96e... --concurrency 4 --max-context-length 262144.
"""

from __future__ import annotations

from typing import Literal

from vibesys_evaluator import Run, main
from vibesys_evaluator.llm_serving import agentx

MODEL = "Qwen/Qwen3.6-35B-A3B"
# The checkpoint revision of reference/meta.json, whose tokenizer AgentX counts tokens with.
TOKENIZER_REVISION = "995ad96eacd98c81ed38be0c5b274b04031597b0"


def replay(run: Run, mode: Literal["full", "smoke"]) -> dict[str, float]:
    # Inside the benchmark's 10,800 s budget, leaving 300 s for the rest of the step.
    return agentx.replay(
        model=MODEL,
        tokenizer=MODEL,
        tokenizer_revision=TOKENIZER_REVISION,
        concurrency=4,
        max_context_length=262144,
        artifacts=run.phase("agentx"),
        mode=mode,
        timeout_secs=10500.0,
    )


def benchmark(run: Run) -> None:
    run.report(replay(run, "full"))


def profile(run: Run) -> None:
    replay(run, "smoke")


if __name__ == "__main__":
    main(metrics=agentx.METRICS, benchmark=benchmark, profile=profile)
