from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from vibesys.input_manifest import load_input_bundle

_REPO = Path(__file__).resolve().parents[2]
_BUNDLE = _REPO / "examples" / "database" / "differential-dataflow"
_PINNED_COMMIT = "4f05cbb61775a45844a0905de9dacfee1e91dd80"


def _load_module(name: str, relative_path: str) -> ModuleType:
    path = _BUNDLE / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_differential_dataflow_bundle_is_a_runnable_database_input() -> None:
    bundle = load_input_bundle(_BUNDLE)

    assert bundle.domain == "database"
    assert bundle.accuracy_command[:4] == (
        "uv",
        "run",
        "python",
        "accuracy_checker/checker.py",
    )
    assert bundle.benchmark_command == (
        "uv",
        "run",
        "python",
        "benchmark/benchmark.py",
    )
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "cpu_seconds"

    assert [(source.name, source.dest) for source in bundle.workspace_sources] == [
        ("engine", "engine"),
        ("ref_engine", "_ref_engine"),
    ]
    assert {source.repo for source in bundle.workspace_sources} == {
        "https://github.com/HQingXuan/differential-dataflow-pinned"
    }
    assert {source.commit for source in bundle.workspace_sources} == {_PINNED_COMMIT}
    assert all(source.strip_git for source in bundle.workspace_sources)


def test_differential_dataflow_objective_matches_benchmark_contract(tmp_path: Path) -> None:
    objectives = tomllib.loads((_BUNDLE / "objectives.toml").read_text(encoding="utf-8"))
    assert objectives == {
        "objective": [{"name": "cpu_seconds", "direction": "min"}],
        "pareto": {"relative_noise": 0.02},
    }

    true_binary = shutil.which("true")
    assert true_binary is not None
    output = tmp_path / "benchmark.json"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_BUNDLE / "benchmark" / "benchmark.py"),
            "--engine-cmd",
            true_binary,
            "--reps",
            "1",
            "--warmups",
            "0",
            "--output-json",
            str(output),
        ],
        cwd=_BUNDLE,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(payload["cpu_seconds"], int | float)
    assert payload["reps"] == 1
    assert payload["warmups"] == 0


def test_attribution_parser_ranks_components_by_instruction_count() -> None:
    profiler = _load_module("differential_dataflow_attribute_parser", "profiler/attribute_cpu.py")
    annotated = """
Ir
--------------------------------------------------------------------------------
file:function
  3,000 ( 75.00%) differential-dataflow/src/trace/implementations/merge_batcher.rs:merge
  1,000 ( 25.00%) differential-dataflow/src/consolidation.rs:consolidate
"""

    components = profiler.aggregate(profiler.parse_annotate(annotated))

    assert [(item["component"], item["ir"], item["pct"]) for item in components] == [
        ("trace/implementations", 3000, 75.0),
        ("consolidation", 1000, 25.0),
    ]


def test_attribution_cli_defaults_to_the_materialized_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiler = _load_module("differential_dataflow_attribute_cli", "profiler/attribute_cpu.py")
    output = tmp_path / "attribution.json"
    observed: dict[str, str] = {}

    monkeypatch.setattr(profiler, "_DEFAULT_ENGINE_BIN", shutil.which("true"))

    def fake_callgrind(binary: str, _args: list[str], _out_file: str) -> tuple[bool, str]:
        observed["binary"] = binary
        return True, "ok"

    monkeypatch.setattr(profiler, "_run_callgrind", fake_callgrind)
    monkeypatch.setattr(
        profiler,
        "_annotate",
        lambda _path: (
            True,
            "file:function\n100 (100.00%) differential-dataflow/src/consolidation.rs:f\n",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["attribute_cpu.py", "--output-json", str(output)])

    assert profiler.main() == 0
    assert observed["binary"] == shutil.which("true")
    assert json.loads(output.read_text(encoding="utf-8"))["components"][0]["component"] == (
        "consolidation"
    )


@pytest.mark.parametrize("gate", ["equivalence", "differential-fuzz"])
def test_reference_oracle_setup_failures_are_fatal_by_default(
    gate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_module(f"differential_dataflow_checker_{gate}", "accuracy_checker/checker.py")
    monkeypatch.setattr(checker, "_run_gate", lambda _name, _engine: 2)
    monkeypatch.setattr(sys, "argv", ["checker.py", "--no-build", "--gates", gate])

    assert checker.main() != 0


def test_sanitizer_rejects_an_unexpected_candidate_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    sanitizer = _load_module(
        "differential_dataflow_sanitizer",
        "accuracy_checker/sanitizer_gate.py",
    )
    monkeypatch.setattr(sanitizer, "_nightly_ok", lambda: (True, "test-target"))
    monkeypatch.setattr(sanitizer.os.path, "exists", lambda _path: True)
    completed = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=7, stdout="", stderr="candidate crashed"),
        ]
    )
    monkeypatch.setattr(sanitizer.subprocess, "run", lambda *_args, **_kwargs: next(completed))
    monkeypatch.setattr(sys, "argv", ["sanitizer_gate.py", "--manifest-path", "engine/Cargo.toml"])

    assert sanitizer.main() != 0


def test_crash_injection_times_out_when_candidate_emits_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crash_gate = _load_module(
        "differential_dataflow_crash_recovery",
        "accuracy_checker/crash_recovery_gate.py",
    )
    monkeypatch.setattr(crash_gate, "_CRASH_INJECTION_TIMEOUT_S", 0.05)

    started = time.monotonic()
    crashed, ok, message = crash_gate._crash_then_restart(  # noqa: SLF001
        sys.executable,
        ["-c", "import time; time.sleep(60)"],
        tmp_path,
        1,
    )

    assert time.monotonic() - started < 2
    assert crashed is False
    assert ok is True
    assert "no stdout" in message
