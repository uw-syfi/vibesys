"""Qwen3.5-9B on one MI210: AgentX replay throughput and latency, golden-logprob accuracy.

check       the golden gate (golden.json: HF transformers bf16 reference, 12 prompts)
benchmark   the scored AgentX replay; vibesys-cli appends --vs-output FILE
profile     a short (smoke) replay, the load a profiler records; never scored
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from vibesys_evaluator import Run, main
from vibesys_evaluator.llm_serving import agentx, golden

MODEL = "Qwen/Qwen3.5-9B"
TOKENIZER_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
GOLDEN = Path(__file__).with_name("golden.json")


def replay(run: Run, mode: Literal["full", "smoke"]) -> dict[str, float]:
    return agentx.replay(
        model=MODEL,
        tokenizer=MODEL,
        tokenizer_revision=TOKENIZER_REVISION,
        concurrency=4,
        max_context_length=262144,
        artifacts=run.phase("agentx"),
        mode=mode,
    )


def check(run: Run) -> str | None:
    return golden.logprobs(GOLDEN, report=run.phase("golden") / "report.json")


def benchmark(run: Run) -> None:
    run.report(replay(run, "full"))


def profile(run: Run) -> None:
    replay(run, "smoke")


if __name__ == "__main__":
    main(metrics=agentx.METRICS, check=check, benchmark=benchmark, profile=profile)
