from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

LINEARIZABLE_QUEUE_INPUTS = {
    "queue-default": "all",
    "queue-spsc": "spsc",
    "queue-mpsc": "mpsc",
    "queue-mpmc": "mpmc",
}

LINEARIZABLE_ACCURACY_SETTINGS = {
    "queue-default": ("24", "50"),
    "queue-spsc": ("32", "100"),
    "queue-mpsc": ("24", "50"),
    "queue-mpmc": ("24", "100"),
}


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "queue-candidate.so", "target"),
    )


def _materialize_linearizable_input(
    project_root: Path,
    input_name: str,
    workspace: Path,
) -> Path:
    from vibe_serve.input_manifest import load_input_bundle
    from vibe_serve.input_project import materialize_input_project

    input_dir = project_root / "examples" / "data-structures" / input_name
    bundle = load_input_bundle(input_dir, project_root=project_root)
    assert bundle.workspace_seed_path is not None
    _copy_input_bundle(bundle.workspace_seed_path, workspace)
    _copy_input_bundle(input_dir, workspace)
    materialize_input_project(
        input_dir,
        workspace,
        project_root=project_root,
        copy_dir=lambda source, target: shutil.copytree(source, target),
    )
    return input_dir


def test_queue_input_core_contract_drives_reference_scenarios(monkeypatch):
    package_src = Path(__file__).parents[1] / "examples" / "libs" / "queue-input-core" / "src"
    monkeypatch.syspath_prepend(str(package_src))

    from queue_input_core.contract import QUEUE_CONTRACTS, SCENARIOS
    from queue_input_core.reference import QueueFactory

    assert SCENARIOS == list(QUEUE_CONTRACTS)
    assert set(SCENARIOS) == {"spsc", "mpsc", "mpmc"}

    for scenario, contract in QUEUE_CONTRACTS.items():
        queue = QueueFactory(scenario, capacity=contract.default_capacity)
        assert queue.capacity == contract.default_capacity


def test_linearizability_workload_uses_contract_worker_counts(monkeypatch):
    package_src = Path(__file__).parents[1] / "examples" / "libs" / "queue-input-core" / "src"
    monkeypatch.syspath_prepend(str(package_src))

    from queue_input_core.contract import get_contract
    from queue_input_core.trace import make_linearizability_workload

    spsc = make_linearizability_workload(
        get_contract("spsc"),
        ops=100,
        producers=8,
        consumers=8,
        seed=1,
    )
    assert spsc.producers == 1
    assert spsc.consumers == 1
    assert len(spsc.clients) == 2

    mpsc = make_linearizability_workload(
        get_contract("mpsc"),
        ops=100,
        producers=3,
        consumers=8,
        seed=1,
    )
    assert mpsc.producers == 3
    assert mpsc.consumers == 1
    assert len(mpsc.clients) == 4


def test_collect_trace_uses_per_thread_events_and_porcupine_shape(monkeypatch):
    package_src = Path(__file__).parents[1] / "examples" / "libs" / "queue-input-core" / "src"
    monkeypatch.syspath_prepend(str(package_src))

    from queue_input_core.contract import get_contract
    from queue_input_core.reference import QueueFactory
    from queue_input_core.trace import (
        collect_trace,
        make_linearizability_workload,
        to_porcupine_history,
    )

    contract = get_contract("mpsc")
    plan = make_linearizability_workload(
        contract,
        ops=40,
        producers=3,
        consumers=1,
        seed=123,
    )
    queue = QueueFactory(contract.name, capacity=64)

    trace = collect_trace(queue, plan, timeout_seconds=5.0)

    assert trace.ok
    assert len(trace.events) == len(plan.clients) * plan.ops_per_client
    enqueued_values = [
        event.input["value"] for event in trace.events if event.operation == "enqueue"
    ]
    assert len(enqueued_values) == len(set(enqueued_values))
    assert sorted(enqueued_values) == list(range(len(enqueued_values)))

    history = to_porcupine_history(trace.events)
    assert set(history[0]) == {"client_id", "input", "output", "call", "return"}


def test_linearizable_queue_manifests_invoke_go_harness_directly():
    root = Path(__file__).parents[1] / "examples" / "data-structures"

    for input_name, scenario in LINEARIZABLE_QUEUE_INPUTS.items():
        manifest = tomllib.loads((root / input_name / "vibeserve.input.toml").read_text())
        operations, trials = LINEARIZABLE_ACCURACY_SETTINGS[input_name]
        expected_suffixes = {
            "accuracy": [
                "run",
                ".",
                "check",
                "--scenario",
                scenario,
                "--operations",
                operations,
                "--trials",
                trials,
            ],
            "benchmark": [
                "run",
                ".",
                "benchmark",
                "--scenario",
                scenario,
                "--repetitions",
                "3",
            ],
        }
        for section, expected_suffix in expected_suffixes.items():
            command = manifest[section]["command"]
            assert command[:2] == ["go", "-C"]
            assert command[3:] == expected_suffix
        result = manifest["benchmark"].get("result")
        if input_name == "queue-default":
            assert result is None
        else:
            assert result == {
                "json_argument": "--output-json",
                "metric": "total_ops_per_sec",
            }

    trusted_wrapper = (
        root.parents[0] / "libs" / "queue-input-core" / "src" / "queue_input_core" / "trusted.py"
    )
    assert not trusted_wrapper.exists()

    queue_core = root.parents[0] / "libs" / "queue-input-core"
    assert (queue_core / "QUEUE_ABI.md").exists()
    assert (queue_core / "include" / "vibeserve_queue_abi.h").exists()
    assert not (queue_core / "QUEUE_PROTOCOL.md").exists()


def test_linearizable_queue_inputs_use_shared_editable_rust_starter():
    from vibe_serve.input_manifest import load_input_bundle

    project_root = Path(__file__).parents[1]
    root = project_root / "examples" / "data-structures"
    starter = project_root / "examples" / "starters" / "queue-copying-rust"
    starter_files = [".gitignore", "Cargo.toml", "Cargo.lock", "Makefile", "src/lib.rs"]

    for relative in starter_files:
        assert (starter / relative).is_file()

    for input_name in LINEARIZABLE_QUEUE_INPUTS:
        input_dir = root / input_name
        bundle = load_input_bundle(input_dir, project_root=project_root)
        assert bundle.workspace_seed_path == starter.resolve()
        assert not (input_dir / "reference" / "reference.py").exists()
        for relative in starter_files:
            assert not (input_dir / relative).exists()

    for input_name in ["queue-lossy", "queue-batch"]:
        assert not (root / input_name / "Cargo.toml").exists()
        manifest = tomllib.loads((root / input_name / "vibeserve.input.toml").read_text())
        assert "workspace" not in manifest


@pytest.mark.parametrize(("input_name", "scenario"), LINEARIZABLE_QUEUE_INPUTS.items())
def test_materialized_rust_starter_builds_and_passes_accuracy(tmp_path, input_name, scenario):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    _materialize_linearizable_input(project_root, input_name, workspace)

    subprocess.run(["make"], cwd=workspace, check=True)
    assert (workspace / "queue-candidate.so").is_file()
    rebuilt = subprocess.run(
        ["make"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "cargo build --release --locked" in rebuilt.stdout

    manifest = tomllib.loads((workspace / "vibeserve.input.toml").read_text())
    accuracy = [
        *manifest["accuracy"]["command"],
        "--capacity",
        "4",
        "--value-size",
        "64",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    completed = subprocess.run(
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    expected = ["spsc", "mpsc", "mpmc"] if scenario == "all" else [scenario]
    for checked_scenario in expected:
        assert f"PASS - {checked_scenario} linearizable" in completed.stdout


def test_materialized_manifest_commands_run_go_harness_directly(tmp_path):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    input_dir = _materialize_linearizable_input(
        project_root,
        "queue-default",
        workspace,
    )
    subprocess.run(["make"], cwd=workspace, check=True)
    manifest = tomllib.loads((input_dir / "vibeserve.input.toml").read_text())

    accuracy = [
        *manifest["accuracy"]["command"],
        "--capacity",
        "4",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)

    output = workspace / "results.json"
    benchmark = [
        *manifest["benchmark"]["command"],
        "--capacity",
        "4",
        "--duration",
        "20ms",
        "--warmup",
        "0s",
        "--output-json",
        str(output),
    ]
    subprocess.run(benchmark, cwd=workspace, check=True)
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["spsc", "mpsc", "mpmc"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


def test_trusted_queue_harness_rejects_adversarial_histories():
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    harness = (
        Path(__file__).parents[1]
        / "examples"
        / "libs"
        / "queue-input-core"
        / "src"
        / "queue_input_core"
        / "trusted_harness"
    )
    subprocess.run(["go", "test", "./..."], cwd=harness, check=True)
