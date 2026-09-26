from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.examples.request_factory_cpu_smoke import load_profile
from tests.support import run_test_command

from vibesys.api.request import load_input_bundle
from vs_evaluator_protocol.api import parse_records, read_measurement

_BUNDLE = Path(__file__).parents[2] / "examples" / "model-serving" / "Llama-3-8B-trn2"
_BENCHMARK = _BUNDLE / "benchmark" / "benchmark.py"


def test_llama3_8b_trn2_uses_request_factory_protocol() -> None:
    bundle = load_input_bundle(_BUNDLE)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == ("benchmark/benchmark.py",)
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


def test_llama3_8b_trn2_cpu_smoke_profile_declares_benchmark_contract() -> None:
    profile = load_profile(_BUNDLE / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == _BUNDLE / "benchmark" / "benchmark.py"
    assert profile.benchmark_args == (
        "--request-count",
        "4",
        "--lengths",
        "16,32",
        "--concurrencies",
        "1,2",
    )
    assert profile.shape_counts == {(16, 16): 8, (32, 32): 8}
    assert profile.response_style == "token-chunks"
    assert profile.disjoint_prompt_batches == (4, 4, 4, 4)
    assert profile.cache_prefix_tokens == 16
    assert profile.required_fields == {
        "temperature": 0,
        "stream": True,
        "ignore_eos": True,
        "return_token_ids": True,
        "stream_options": {"include_usage": True},
    }
    assert profile.failure_message_contains == "success_steps"
    assert set(profile.metrics) == {"aggregate_throughput"}


def test_cpu_smoke_profile_rejects_nonpositive_prompt_batches(tmp_path: Path) -> None:
    profile_path = _BUNDLE / "benchmark" / "cpu_smoke.toml"
    invalid = tmp_path / "cpu_smoke.toml"
    invalid.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "disjoint_prompt_batches = [4, 4, 4, 4]",
            "disjoint_prompt_batches = [0, 4, 4, 4]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="greater than 0"):
        load_profile(invalid)


def test_matrix_emits_valid_protocol_and_partitions_rf_prompt_ordinals(tmp_path: Path) -> None:
    engine = tmp_path / "fake-rf-engine"
    state_path = engine.with_suffix(".state.json")
    engine.write_text(
        """#!/usr/bin/env python3
import csv
import json
import sys
from pathlib import Path

args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
rows = list(csv.DictReader(Path(value("--trace")).open(encoding="utf-8")))
shard_count = int(value("--shard-count"))
shard_index = int(value("--shard-index"))
selected = rows[shard_index::shard_count]
state_path = Path(sys.argv[0]).with_suffix(".state.json")
state = json.loads(state_path.read_text()) if state_path.exists() else {"ids": [], "pool": None}
selected_ids = [row["id"] for row in selected]
if set(selected_ids) & set(state["ids"]):
    raise SystemExit("matrix points reused RF workload ordinals")
pool = int(value("--token-pool-limit"))
if state["pool"] not in (None, pool):
    raise SystemExit("matrix points used different synthetic token pools")
state_path.write_text(json.dumps({"ids": state["ids"] + selected_ids, "pool": pool}))
length = int(selected[0]["input_len"])
common = {
    "attempted_steps": len(selected),
    "success_steps": len(selected),
    "failed_steps": 0,
    "output_mismatch_steps": 0,
    "output_token_throughput_per_s": float(length),
    "request_throughput_per_s": 1.0,
    "ttft_ms_p50": 1.0,
    "ttft_ms_p90": 1.0,
    "tpot_ms_p50": 1.0,
    "tpot_ms_p90": 1.0,
}
Path(value("--summary-path")).write_text(json.dumps({"replay": {"common": common}}))
with Path(value("--log-path")).open("w", encoding="utf-8") as output:
    for row in selected:
        record = {
            "source": {"data": {"input_len": length, "output_len_target": length}},
            "outcome": {"error": None, "output_len_actual": length},
        }
        output.write(json.dumps(record) + "\\n")
""",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    output = tmp_path / "result.jsonl"

    completed = run_test_command(
        [
            sys.executable,
            str(_BENCHMARK),
            "--request-factory-engine",
            str(engine),
            "--request-count",
            "2",
            "--lengths",
            "2,4",
            "--concurrencies",
            "1,2",
            "--vs-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values == {"aggregate_throughput": 4.0}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["ids"]) == 8
    assert len(set(state["ids"])) == 8
