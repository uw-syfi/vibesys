"""Explicitly regenerate the portable-record goldens for a reviewed codec change."""

import json
from pathlib import Path

from vibesys.hypothesis.history import RoundRecord, parse_round_record, serialize_round_record

out = Path(__file__).parent
out.mkdir(parents=True, exist_ok=True)
cases = {
    "current": RoundRecord(
        round_number=3,
        commit="a" * 40,
        perf_metric=112.5,
        perf_unit="tok_s",
        passed=True,
        hypothesis_id="H-03",
        hypothesis_declared_outcome="nominated",
        judge_verdict="pass",
        hypothesis_outcome="proven",
        hypothesis_claim="batching reduces launch overhead",
        hypothesis_task="batch prefill",
        hypothesis_parent_round=2,
        hypothesis_parent_commit="b" * 40,
        metrics={"throughput": 112.5, "latency": 2.0},
        evaluation_artifact="evaluations/round-3.json",
        official_evaluation=True,
        official_evaluation_reason="cadence",
        candidate_disposition="pareto_frontier",
        candidate_metrics={"throughput": 112.5, "latency": 2.0},
        candidate_evaluation_artifact="evaluations/candidate-3.json",
        candidate_operating_point="batch=4",
        candidate_retention_reason="throughput gain",
        candidate_retained=True,
        perf_direction="max",
        perf_baseline_round=2,
        perf_baseline_commit="b" * 40,
        perf_baseline_metric=100.0,
        perf_delta_pct=12.5,
        perf_comparison="better",
        perf_provenance="framework",
        implementer_driver="agentshim",
        implementer_provider="codex",
        implementer_model="gpt-5.6-sol",
        attempts=2,
    ),
    "legacy": parse_round_record(
        {
            "round": 1,
            "commit": None,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
            "reviewed": False,
            "hypothesis_outcome": "retired-outcome",
            "candidate_disposition": "retired-disposition",
        }
    ),
    "deferred": RoundRecord(
        round_number=4,
        commit="c" * 40,
        perf_metric=107.0,
        perf_unit="tok_s",
        passed=False,
        reviewed=False,
        judge_verdict="pass",
        hypothesis_outcome="continue",
        profile_skipped=True,
        perf_provenance="implementer",
        perf_comparison="incomparable",
    ),
}
for name, record in cases.items():
    (out / f"{name}.json").write_text(json.dumps(serialize_round_record(record), indent=2) + "\n")
(out / "legacy-input.json").write_text(
    json.dumps(
        {
            "round": 1,
            "commit": None,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
            "reviewed": False,
            "hypothesis_outcome": "retired-outcome",
            "candidate_disposition": "retired-disposition",
        },
        indent=2,
    )
    + "\n"
)
