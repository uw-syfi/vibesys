from __future__ import annotations

from pathlib import Path


def test_queue_input_core_contract_drives_reference_scenarios(monkeypatch):
    package_src = Path(__file__).parents[1] / "examples" / "libs" / "queue-input-core" / "src"
    monkeypatch.syspath_prepend(str(package_src))

    from queue_input_core.contract import QUEUE_CONTRACTS, SCENARIOS
    from queue_input_core.reference import QueueFactory

    assert SCENARIOS == list(QUEUE_CONTRACTS)

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


def test_focused_queue_inputs_do_not_import_core_main():
    root = Path(__file__).parents[1] / "examples" / "data-structures"
    focused_inputs = ["queue-spsc", "queue-mpsc", "queue-mpmc", "queue-lossy", "queue-batch"]

    for input_name in focused_inputs:
        for rel_path in ["accuracy_checker/checker.py", "benchmark/benchmark.py"]:
            text = (root / input_name / rel_path).read_text()
            assert "SCENARIO =" in text
            assert "import main" not in text
            assert "--scenario" not in text
