from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


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
    expected_scenarios = {
        "queue-default": "all",
        "queue-spsc": "spsc",
        "queue-mpsc": "mpsc",
        "queue-mpmc": "mpmc",
    }

    for input_name, scenario in expected_scenarios.items():
        manifest = tomllib.loads((root / input_name / "vibeserve.input.toml").read_text())
        for section, action in [("accuracy", "check"), ("benchmark", "benchmark")]:
            command = manifest[section]["command"]
            assert command[:2] == ["go", "-C"]
            assert command[3:] == ["run", ".", action, "--scenario", scenario]

    trusted_wrapper = (
        root.parents[0] / "libs" / "queue-input-core" / "src" / "queue_input_core" / "trusted.py"
    )
    assert not trusted_wrapper.exists()


def test_materialized_manifest_commands_run_go_harness_directly(tmp_path):
    if shutil.which("go") is None:
        pytest.skip("Go is required by the trusted queue evaluator")

    from vibe_serve.input_project import materialize_input_project

    project_root = Path(__file__).parents[1]
    input_dir = project_root / "examples" / "data-structures" / "queue-default"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    materialize_input_project(
        input_dir,
        workspace,
        project_root=project_root,
        copy_dir=lambda source, target: shutil.copytree(source, target),
    )
    manifest = tomllib.loads((input_dir / "vibeserve.input.toml").read_text())

    accuracy = [
        *manifest["accuracy"]["command"],
        "--use-reference",
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
        "--use-reference",
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


def test_trusted_queue_harness_rejects_adversarial_histories():
    if shutil.which("go") is None:
        pytest.skip("Go is required by the trusted queue evaluator")

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
