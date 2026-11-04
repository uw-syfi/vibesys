"""Public AgentX evaluator contract: replay policy, result validation and scoring."""

from __future__ import annotations

import copy
import json
import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task

from vs_evaluator_protocol.api import Measurement, parse_records, read_measurement

ROOT = Path(__file__).parents[2]
EVALUATOR = ROOT / "resources/evaluators/agentx/agentx_eval.py"
PROJECT = ROOT / "examples/model-serving/qwen3.5-9b-mi210"
# run_path loads the standalone public CLI without modifying its globals or patching modules.
API = SimpleNamespace(**runpy.run_path(str(EVALUATOR)))


def parse_profile(argv: list[str]) -> object:
    manifest = tomllib.loads((PROJECT / ".vibesys/tasks/agentx/vibesys.input.toml").read_text())
    return API.parse_args([*manifest["benchmark"]["args"], *argv])


def valid_export() -> dict[str, Any]:
    return {
        "metadata": {"scenario": "agentx", "submission_valid": True},
        "request_count": {"unit": "requests", "avg": 12},
        "error_request_count": {"unit": "requests", "avg": 0},
        "error_summary": [],
        "output_token_throughput": {"unit": "tokens/sec", "avg": 123},
        "request_latency": {"unit": "ms", "p90": 450},
        "time_to_first_token": {"unit": "ms", "p50": 10, "p90": 20},
        "inter_token_latency": {"unit": "ms", "p50": 3, "p90": 5},
    }


class FakeProfileRunner:
    """Materialize an upstream export at the destination selected by the CLI."""

    def __init__(
        self, summary: object, *, error: Exception | None = None, exports: int = 1
    ) -> None:
        self.summary = summary
        self.error = error
        self.exports = exports

    def __call__(self, command: list[str], timeout: int) -> None:
        assert timeout > 0
        assert command[1:5] == ["-I", "-m", "aiperf", "profile"]
        if self.error is not None:
            raise self.error
        destination = Path(command[command.index("--output-artifact-dir") + 1])
        for index in range(self.exports):
            run_dir = destination / str(index)
            run_dir.mkdir(parents=True)
            (run_dir / "profile_export_aiperf.json").write_text(json.dumps(self.summary))


def test_task_matches_the_five_axis_qwen_frontier() -> None:
    bundle = load_example_task(PROJECT, "agentx")
    assert bundle.reference_path is not None
    metadata = json.loads((bundle.reference_path / "meta.json").read_text())
    args = bundle.manifest.benchmark.args
    assert metadata["revision"] == args[args.index("--tokenizer-revision") + 1]
    assert metadata["model_id"] == args[args.index("--model") + 1]
    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-agentx"
    assert bundle.manifest.benchmark.entrypoint == "agentx-v1"
    assert bundle.manifest.benchmark.result_protocol == 2
    objectives = tomllib.loads((PROJECT / ".vibesys/tasks/agentx/objectives.toml").read_text())
    assert {row["name"]: row["direction"] for row in objectives["objective"]} == {
        "output_tokens_per_s": "max",
        "ttft_p50_ms": "min",
        "ttft_p90_ms": "min",
        "tpot_p50_ms": "min",
        "tpot_p90_ms": "min",
    }
    assert objectives["pareto"]["relative_noise"] == 0.03


def test_full_recipe_is_pinned_and_smoke_is_explicitly_unsafe(tmp_path: Path) -> None:
    full = API.profile_command(parse_profile([]), tmp_path)
    smoke = API.profile_command(parse_profile(["--mode", "smoke"]), tmp_path)
    assert full[full.index("--public-dataset") + 1] == API.DATASET
    assert full[full.index("--benchmark-duration") + 1] == "900"
    assert full[full.index("--max-context-length") + 1] == "262144"
    assert "--unsafe-override" not in full
    assert "--num-dataset-entries" not in full
    assert "--unsafe-override" in smoke
    assert smoke[smoke.index("--benchmark-duration") + 1] == "60"


@given(value=st.floats(min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False))
def test_latency_percentiles_keep_units_and_values(value: float) -> None:
    summary = valid_export()
    summary["inter_token_latency"]["p90"] = value
    assert API.summary_metrics(summary)["tpot_p90_ms"] == value


@given(
    field=st.sampled_from(
        [
            ("output_token_throughput", "avg"),
            ("request_latency", "p90"),
            ("time_to_first_token", "p50"),
            ("time_to_first_token", "p90"),
            ("inter_token_latency", "p50"),
            ("inter_token_latency", "p90"),
        ]
    ),
    bad=st.one_of(
        st.none(),
        st.booleans(),
        st.text(),
        st.floats(max_value=-1e-10),
        st.sampled_from([float("nan"), float("inf")]),
    ),
)
def test_invalid_values_never_become_metrics(field: tuple[str, str], bad: object) -> None:
    summary = valid_export()
    summary[field[0]][field[1]] = bad
    with pytest.raises((ValueError, TypeError)):
        API.summary_metrics(summary)


@pytest.mark.parametrize(
    "metric",
    [
        key
        for key, value in valid_export().items()
        if isinstance(value, dict) and "unit" in value and key != "error_request_count"
    ],
)
def test_missing_metrics_and_incorrect_units_fail(metric: str) -> None:
    summary = valid_export()
    del summary[metric]
    with pytest.raises(ValueError, match=metric):
        API.summary_metrics(summary)
    summary = valid_export()
    summary[metric]["unit"] = "wrong-unit"
    with pytest.raises(ValueError, match=metric):
        API.summary_metrics(summary)


@pytest.mark.parametrize(
    "metadata", [None, {}, {"scenario": "other"}, {"scenario": "agentx", "submission_valid": False}]
)
def test_missing_or_invalid_submission_fails(metadata: object) -> None:
    summary = valid_export()
    summary["metadata"] = metadata
    with pytest.raises(ValueError, match="AgentX"):
        API.summary_metrics(summary)


@pytest.mark.parametrize(
    ("metric", "value"),
    [("error_request_count", 1), ("request_count", 0), ("output_token_throughput", 0)],
)
def test_failed_or_empty_work_is_not_success(metric: str, value: int) -> None:
    summary = valid_export()
    summary[metric]["avg"] = value
    with pytest.raises(ValueError, match="AgentX"):
        API.summary_metrics(summary)


def test_valid_run_produces_one_measurement_with_every_objective(tmp_path: Path) -> None:
    output = tmp_path / "result.jsonl"
    profile = parse_profile(["--vs-output", str(output)])
    assert API.evaluate(profile, FakeProfileRunner(valid_export())) == 0
    measurement = read_measurement(parse_records(output.read_text()))
    assert isinstance(measurement, Measurement)
    assert measurement.values is not None
    assert dict(measurement.values) == {
        "output_tokens_per_s": 123,
        "p90_latency_ms": 450,
        "ttft_p50_ms": 10,
        "ttft_p90_ms": 20,
        "tpot_p50_ms": 3,
        "tpot_p90_ms": 5,
    }


def test_smoke_succeeds_but_cannot_produce_a_scored_measurement(tmp_path: Path) -> None:
    output = tmp_path / "smoke.jsonl"
    summary = valid_export()
    summary["metadata"]["submission_valid"] = False
    profile = parse_profile(["--mode", "smoke", "--vs-output", str(output)])
    assert API.evaluate(profile, FakeProfileRunner(summary)) == 0
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["hello", "error"]
    assert "unscored" in records[-1]["message"]


@pytest.mark.parametrize(
    "summary", [[], {}, {"was_cancelled": True}, {"error_request_count": {"avg": 1}}]
)
def test_bad_exports_produce_errors_without_results(summary: object, tmp_path: Path) -> None:
    output = tmp_path / "result.jsonl"
    profile = parse_profile(["--vs-output", str(output)])
    assert API.evaluate(profile, FakeProfileRunner(summary)) == 1
    assert [record["kind"] for record in map(json.loads, output.read_text().splitlines())] == [
        "hello",
        "error",
    ]


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing harness"),
        API.ProfileError("harness deadline exceeded"),
        API.ProfileError("harness exited with status 1"),
    ],
)
def test_launcher_failure_is_reported_without_score(error: Exception, tmp_path: Path) -> None:
    output = tmp_path / "result.jsonl"
    profile = parse_profile(["--vs-output", str(output)])
    runner = FakeProfileRunner(None, error=error)
    assert API.evaluate(profile, runner) == 1
    assert str(error) in output.read_text()
    assert '"kind": "result"' not in output.read_text()


def test_stale_artifacts_are_rejected(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "profile_export_aiperf.json").write_text(json.dumps(valid_export()))
    before = copy.deepcopy(valid_export())
    assert (
        API.evaluate(parse_profile(["--artifact-dir", str(artifacts)]), FakeProfileRunner(before))
        == 1
    )


@given(value=st.integers(max_value=0))
def test_nonpositive_concurrency_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        parse_profile(["--concurrency", str(value)])


def test_unknown_replay_overrides_are_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_profile(["--ignore-trace-delays"])


@pytest.mark.parametrize("exports", [0, 2])
def test_missing_or_ambiguous_export_is_not_a_result(exports: int, tmp_path: Path) -> None:
    output = tmp_path / "result.jsonl"
    profile = parse_profile(["--vs-output", str(output)])
    assert API.evaluate(profile, FakeProfileRunner(valid_export(), exports=exports)) == 1
    assert '"kind": "result"' not in output.read_text()


def test_mutable_tokenizer_revision_is_rejected() -> None:
    with pytest.raises(ValueError, match="tokenizer_revision"):
        parse_profile(["--tokenizer-revision", "main"])


def test_upstream_null_error_counter_means_no_errors() -> None:
    summary = valid_export()
    summary["error_request_count"] = None
    assert API.summary_metrics(summary)["output_tokens_per_s"] == 123
    summary["error_summary"] = [{"error": "failed request"}]
    with pytest.raises(ValueError, match="error_summary"):
        API.summary_metrics(summary)


def test_omitted_error_counter_needs_an_explicit_empty_error_summary() -> None:
    summary = valid_export()
    del summary["error_request_count"]
    assert API.summary_metrics(summary)["output_tokens_per_s"] == 123
    del summary["error_summary"]
    with pytest.raises(ValueError, match="error_summary"):
        API.summary_metrics(summary)


def test_live_agentx_smoke_export_maps_all_axes_but_is_not_a_full_result() -> None:
    fixture = ROOT / "tests/examples/fixtures/agentx_smoke_export.json"
    summary = json.loads(fixture.read_text())
    values = API.summary_metrics(summary, smoke=True)
    assert values["output_tokens_per_s"] > 0
    assert len(values) == 6
    with pytest.raises(ValueError, match="submission invalid"):
        API.summary_metrics(summary)


@pytest.mark.parametrize("smoke", [False, True])
def test_cancelled_export_is_rejected_in_both_modes(*, smoke: bool) -> None:
    summary = valid_export()
    summary["was_cancelled"] = True
    with pytest.raises(ValueError, match="cancelled"):
        API.summary_metrics(summary, smoke=smoke)
