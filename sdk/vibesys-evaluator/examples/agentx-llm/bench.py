"""agentx-llm: an LLM serving task on the domain-free core (spec example, version "0.2" task).

The candidate is an OpenAI-compatible server that vibesys-cli starts from the
candidate's own image; this script runs in the task image (FROM
vibesys-evaluator-llm-serving) and reaches it at VIBESYS_SERVICE_URL.

    python /task/bench.py check                     golden-logprob accuracy gate
    python /task/bench.py benchmark --vs-output F   AgentX replay, six metrics
    python /task/bench.py profile                   a short replay for a profiler

The task directory holds golden.json beside this file: reference token ids,
greedy continuations and teacher-forced logprobs of the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from vibesys_evaluator import Run, main
from vibesys_evaluator.llm_serving import agentx, endpoint, golden

MODEL = "Qwen/Qwen3.5-9B"
REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
GOLDEN = Path(__file__).with_name("golden.json")
# The calibrated limits; spelled out here, they are also golden.Thresholds()'s defaults.
LIMITS = golden.Thresholds(near_tie_margin=0.5, max_mean_abs_dlogprob=0.05)


def replay(run: Run, mode: Literal["full", "smoke"]) -> dict[str, float]:
    return agentx.replay(
        model=MODEL,
        tokenizer=MODEL,
        tokenizer_revision=REVISION,
        concurrency=4,
        max_context_length=262144,
        artifacts=run.phase("agentx"),
        url=endpoint(),
        mode=mode,
    )


def check(run: Run) -> str | None:
    return golden.logprobs(GOLDEN, thresholds=LIMITS, report=run.phase("golden") / "report.json")


def benchmark(run: Run) -> None:
    run.report(replay(run, "full"))


def profile(run: Run) -> None:
    print(f"profile load: agentx-harness {agentx.HARNESS_VERSION}, smoke replay")
    replay(run, "smoke")


if __name__ == "__main__":
    main(metrics=agentx.METRICS, check=check, benchmark=benchmark, profile=profile)
