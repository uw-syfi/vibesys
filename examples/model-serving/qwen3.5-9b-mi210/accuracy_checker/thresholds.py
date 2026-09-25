"""Gate thresholds, shared by the base checks (`checker.py`) and the resume check (`resume.py`)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    """Fixed before any optimized engine was evaluated; calibration evidence in README.md."""

    # A golden position whose HF top-1 beats top-2 by less than this (nats) is a near-tie.
    near_tie_margin: float = 0.5
    # Teacher-forced: candidate argmax must equal HF top-1 at every non-near-tie position.
    max_decisive_flips: int = 0
    # Teacher-forced |candidate logprob - HF logprob| of the golden token, over all positions.
    max_mean_abs_dlogprob: float = 0.05
    max_p99_abs_dlogprob: float = 0.25
    # Free-running greedy: every divergence from golden must happen at a near-tie position ...
    # ... and the mean matched-prefix fraction across prompts must stay above this floor.
    min_mean_prefix_fraction: float = 0.5
