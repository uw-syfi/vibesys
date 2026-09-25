"""Pure transitions for the authoritative agent-run hypothesis state.

# TODO(stack PR 07): remove. Glue re-export: definitions now live in
# ``vibesys.search.hypothesis.transitions``.
"""

from __future__ import annotations

from vibesys.search.hypothesis.transitions import (
    ResolutionEvidence,
    adopt_metric_space,
    append_round,
    apply_strategy_updates,
    finish_hypothesis,
    measurement_delta_reason,
    metric_baseline,
    project_round_evidence,
    record_metric_value,
    reproject_run_evidence,
    resolve_hypothesis_outcome,
    scalar_candidate_retained,
    start_hypothesis,
    trusted_perf_provenance,
    update_active_hypothesis,
)

__all__ = [
    "ResolutionEvidence",
    "adopt_metric_space",
    "append_round",
    "apply_strategy_updates",
    "finish_hypothesis",
    "measurement_delta_reason",
    "metric_baseline",
    "project_round_evidence",
    "record_metric_value",
    "reproject_run_evidence",
    "resolve_hypothesis_outcome",
    "scalar_candidate_retained",
    "start_hypothesis",
    "trusted_perf_provenance",
    "update_active_hypothesis",
]
