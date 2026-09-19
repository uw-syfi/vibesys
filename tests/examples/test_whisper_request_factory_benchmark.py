from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict, cast

from vibesys.input_manifest import load_input_bundle

_REPO_ROOT = Path(__file__).parents[2]
_TASK_ROOT = _REPO_ROOT / "examples" / "model-serving" / "whisper-large-v3"
_TRACE_PATH = _TASK_ROOT / "benchmark" / "requests.jsonl"
_CLIPS = (
    ("sample1.wav", "5bc474ec55cd50e7192d793a63c1b456fd205ea05737fc3fdffb99f46f61500a"),
    ("sample2.wav", "176552bf16159cbdadd0ca838a26dc85356984cd191c6e792ffcb879d814ac00"),
    ("sample3.wav", "3fd51bc4cee9b2b23c204f6034c049c1ebc67d404d67d0815a4a440aacc93190"),
    ("sample4.wav", "5b91b2fc1c6db455e4e4873c80ab7831538897e9ad3227e58edd2c2c07ced772"),
)


class _Asset(TypedDict):
    path: str
    sha256: str
    media_type: str


class _Input(TypedDict):
    type: str
    asset: _Asset


class _Output(TypedDict):
    type: str
    max_tokens: int


class _TraceRow(TypedDict):
    id: str
    arrival_time_ms: float
    inputs: list[_Input]
    outputs: list[_Output]


def _trace_rows() -> list[_TraceRow]:
    return [
        cast("_TraceRow", json.loads(line))
        for line in _TRACE_PATH.read_text(encoding="utf-8").splitlines()
    ]


def _option(arguments: tuple[str, ...], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def test_trace_is_a_deterministic_64_request_audio_cycle() -> None:
    rows = _trace_rows()

    assert len(rows) == 64
    assert len({row["id"] for row in rows}) == 64
    for index, row in enumerate(rows):
        filename, expected_sha256 = _CLIPS[index % len(_CLIPS)]
        assert row["id"] == f"measure-{index:06d}-{Path(filename).stem}"
        assert row["arrival_time_ms"] == 0.0
        assert row["outputs"] == [{"type": "text", "max_tokens": 448}]
        [audio] = row["inputs"]
        assert audio["type"] == "audio"
        asset = audio["asset"]
        assert asset == {
            "path": f"../test_audio/{filename}",
            "sha256": expected_sha256,
            "media_type": "audio/wav",
        }
        asset_path = (_TRACE_PATH.parent / asset["path"]).resolve()
        assert asset_path.is_file()
        assert hashlib.sha256(asset_path.read_bytes()).hexdigest() == expected_sha256


def test_manifest_runs_the_pinned_engine_directly_and_reads_its_summary() -> None:
    bundle = load_input_bundle(_TASK_ROOT)
    manifest = bundle.manifest

    assert manifest.evaluator is not None
    assert manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert manifest.evaluator.version == "0.1.0"
    assert manifest.benchmark.entrypoint == "request-factory-engine"
    arguments = manifest.benchmark.args
    assert _option(arguments, "--trace") == "benchmark/requests.jsonl"
    assert _option(arguments, "--input-file-format") == "multimodal-independent-v1"
    assert _option(arguments, "--base-url") == "http://localhost:8000/v1"
    assert _option(arguments, "--model") == "whisper-large-v3"
    assert _option(arguments, "--backend") == "openai-transcriptions"
    assert _option(arguments, "--dialect") == "openai"
    assert _option(arguments, "--temperature") == "0"
    assert _option(arguments, "--arrival-mode") == "saturated"
    assert _option(arguments, "--max-concurrency") == "8"
    assert _option(arguments, "--request-log") == "false"
    assert _option(arguments, "--timeline") == "false"
    assert manifest.benchmark.result is not None
    assert manifest.benchmark.result.json_argument == "--summary-path"
    assert manifest.benchmark.result.metric == "request_throughput_per_s"
    assert not (_TASK_ROOT / "benchmark" / "benchmark.py").exists()
    assert bundle.benchmark_command[-len(arguments) :] == arguments
