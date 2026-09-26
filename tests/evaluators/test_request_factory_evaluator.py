"""Tests for the bundled Request Factory evaluator entrypoints."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vibesys.evaluators import (
    EvaluatorPackageRequirement,
    resolve_evaluator_package,
    tool_token,
)
from vs_evaluator_protocol.api import parse_records, read_measurement

if TYPE_CHECKING:
    import subprocess
    from types import ModuleType

_PACKAGE_ROOT = Path(__file__).parents[2] / "resources" / "evaluators" / "request-factory"
_FIXED_TEXT = _PACKAGE_ROOT / "fixed_text.py"
_FIXTURE_TOKENIZER = (
    Path(__file__).parents[1] / "examples" / "fixtures" / "request_factory_tokenizer.json"
)
_TEST_REVISION = "1111111111111111111111111111111111111111"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"request_factory_{name}", _PACKAGE_ROOT / name)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engine_entrypoint_is_the_direct_tool_command() -> None:
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(
            name="vibesys-evaluator-request-factory",
            version="0.1.0",
        )
    )
    engine_arguments = [
        "--trace",
        "trace.jsonl",
        "--model",
        "m",
        "--input-file-format",
        "multimodal-independent-v1",
        "--dry-run",
    ]
    assert package.command("request-factory-engine", *engine_arguments) == (
        tool_token("request-factory", "session_runner"),
        *engine_arguments,
    )


def test_fixed_text_entrypoint_forwards_the_typed_workload_and_emits_protocol_v2(
    tmp_path: Path,
) -> None:
    engine = _write_engine(tmp_path, _valid_summary(request_count=7))
    output = tmp_path / "result.jsonl"
    tokenizer = (
        tmp_path
        / "hf"
        / "hub"
        / "models--org--tokenizer"
        / "snapshots"
        / _TEST_REVISION
        / "tokenizer.json"
    )
    tokenizer.parent.mkdir(parents=True)
    tokenizer.write_text("{}", encoding="utf-8")

    completed = run_test_command(
        [
            sys.executable,
            str(_FIXED_TEXT),
            "--request-factory-engine",
            str(engine),
            "--model",
            "test-model",
            "--tokenizer",
            "org/tokenizer",
            "--tokenizer-revision",
            _TEST_REVISION,
            "--request-count",
            "7",
            "--input-tokens",
            "19",
            "--output-tokens",
            "5",
            "--concurrency",
            "3",
            "--url",
            "http://server:9000",
            "--vs-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HF_HOME": str(tmp_path / "hf")},
    )

    assert completed.returncode == 0, completed.stderr
    capture = json.loads(engine.with_suffix(".json").read_text(encoding="utf-8"))
    assert capture["max_concurrency"] == "3"
    assert capture["base_url"] == "http://server:9000/v1"
    assert capture["model"] == "test-model"
    assert capture["tokenizer"] == str(tokenizer)
    assert capture["token_pool_limit"] == str(2 * 19)
    assert len(capture["trace_rows"]) == 8
    assert capture["trace_rows"][1].endswith(",0,19,5")
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.failure is None
    assert measurement.values == {
        "output_token_throughput_per_s": 123.0,
        "p90_latency_ms": 45.0,
    }


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        ([], "summary must be an object"),
        ({}, "independent_requests"),
        (
            {
                "replay": {
                    "kind": "independent_requests",
                    "common": {
                        "failed_steps": 0,
                        "attempted_steps": 1,
                        "success_steps": 1,
                        "output_mismatch_steps": 1,
                        "output_token_throughput_per_s": 1.0,
                        "total_duration_ms_p90": 1.0,
                    },
                }
            },
            "output_mismatch_steps",
        ),
        (
            {
                "replay": {
                    "kind": "independent_requests",
                    "common": {
                        "failed_steps": 0,
                        "attempted_steps": 1,
                        "success_steps": 1,
                        "output_mismatch_steps": 0,
                        "output_token_throughput_per_s": "fast",
                        "total_duration_ms_p90": 1.0,
                    },
                }
            },
            "not numeric",
        ),
    ],
)
def test_fixed_text_entrypoint_rejects_malformed_or_incomplete_summaries(
    tmp_path: Path, summary: object, message: str
) -> None:
    engine = _write_engine(tmp_path, summary)
    output = tmp_path / "result.jsonl"

    completed = _run_fixed_text(engine, output)

    assert completed.returncode == 1
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values is None
    assert measurement.failure is not None
    assert message in measurement.failure


def test_fixed_text_entrypoint_rejects_an_undersized_token_pool_warning(tmp_path: Path) -> None:
    engine = _write_engine(
        tmp_path,
        _valid_summary(request_count=1),
        stderr="warning: token pool (1 tokens) is smaller than the longest prompt\n",
    )
    output = tmp_path / "result.jsonl"

    completed = _run_fixed_text(engine, output)

    assert completed.returncode == 1
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values is None
    assert measurement.failure is not None
    assert "token pool is shorter" in measurement.failure


def test_fixed_text_entrypoint_rejects_an_unpinned_remote_tokenizer(tmp_path: Path) -> None:
    output = tmp_path / "result.jsonl"

    completed = run_test_command(
        [
            sys.executable,
            str(_FIXED_TEXT),
            "--request-factory-engine",
            str(tmp_path / "unused-engine"),
            "--model",
            "test-model",
            "--tokenizer",
            "org/tokenizer",
            "--request-count",
            "1",
            "--input-tokens",
            "2",
            "--output-tokens",
            "1",
            "--concurrency",
            "1",
            "--vs-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "remote --tokenizer requires --tokenizer-revision" in completed.stderr
    assert not output.exists()


def _valid_summary(*, request_count: int) -> dict[str, object]:
    return {
        "replay": {
            "kind": "independent_requests",
            "common": {
                "failed_steps": 0,
                "attempted_steps": request_count,
                "success_steps": request_count,
                "output_mismatch_steps": 0,
                "output_token_throughput_per_s": 123.0,
                "total_duration_ms_p90": 45.0,
            },
        }
    }


def _write_engine(tmp_path: Path, summary: object, *, stderr: str = "") -> Path:
    engine = tmp_path / "session_runner"
    engine.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "def value(flag): return args[args.index(flag) + 1]\n"
        "capture = {\n"
        "  'max_concurrency': value('--max-concurrency'),\n"
        "  'base_url': value('--base-url'),\n"
        "  'model': value('--model'),\n"
        "  'tokenizer': value('--tokenizer'),\n"
        "  'token_pool_limit': value('--token-pool-limit'),\n"
        "  'trace_rows': Path(value('--trace')).read_text().splitlines(),\n"
        "}\n"
        "Path(__file__).with_suffix('.json').write_text(json.dumps(capture))\n"
        f"Path(value('--summary-path')).write_text({json.dumps(json.dumps(summary))})\n"
        f"sys.stderr.write({stderr!r})\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    return engine


def _run_fixed_text(engine: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return run_test_command(
        [
            sys.executable,
            str(_FIXED_TEXT),
            "--request-factory-engine",
            str(engine),
            "--model",
            "test-model",
            "--tokenizer",
            str(_FIXTURE_TOKENIZER),
            "--request-count",
            "1",
            "--input-tokens",
            "2",
            "--output-tokens",
            "1",
            "--concurrency",
            "1",
            "--vs-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_task_adapter_injects_engine_before_task_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _load_script("adapter.py")
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(adapter.os, "execv", lambda path, argv: captured.append((path, argv)))

    assert (
        adapter.main(
            ["--engine", "/trusted/session_runner", "--", "benchmark.py", "--url", "server"]
        )
        == 0
    )
    assert captured == [
        (
            sys.executable,
            [
                sys.executable,
                "benchmark.py",
                "--request-factory-engine",
                "/trusted/session_runner",
                "--url",
                "server",
            ],
        )
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--engine", "/trusted/session_runner"],
        ["--engine", "/trusted/session_runner", "--"],
        ["--engine", "/trusted/session_runner", "benchmark.py"],
    ],
)
def test_task_adapter_rejects_missing_separator_or_script(arguments: list[str]) -> None:
    adapter = _load_script("adapter.py")

    with pytest.raises(ValueError, match=r"usage: adapter\.py"):
        adapter.main(arguments)
