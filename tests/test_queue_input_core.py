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
