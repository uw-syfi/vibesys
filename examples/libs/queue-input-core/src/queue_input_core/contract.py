from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueContract:
    name: str
    description: str
    default_capacity: int
    default_producers: int
    default_consumers: int
    configurable_producers: bool
    configurable_consumers: bool


QUEUE_CONTRACTS: dict[str, QueueContract] = {
    "spsc": QueueContract(
        name="spsc",
        description="single-producer single-consumer bounded FIFO",
        default_capacity=1024,
        default_producers=1,
        default_consumers=1,
        configurable_producers=False,
        configurable_consumers=False,
    ),
    "mpmc": QueueContract(
        name="mpmc",
        description="multi-producer multi-consumer bounded FIFO",
        default_capacity=1024,
        default_producers=4,
        default_consumers=4,
        configurable_producers=True,
        configurable_consumers=True,
    ),
    "mpsc": QueueContract(
        name="mpsc",
        description="multi-producer single-consumer bounded FIFO",
        default_capacity=1024,
        default_producers=4,
        default_consumers=1,
        configurable_producers=True,
        configurable_consumers=False,
    ),
}

SCENARIOS = list(QUEUE_CONTRACTS)


def get_contract(scenario: str) -> QueueContract:
    try:
        return QUEUE_CONTRACTS[scenario]
    except KeyError as exc:
        raise ValueError(f"Unknown queue scenario: {scenario}") from exc
