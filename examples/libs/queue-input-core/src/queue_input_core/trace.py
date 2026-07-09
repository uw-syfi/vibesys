from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from queue_input_core.contract import QueueContract


@dataclass(frozen=True)
class PlannedOperation:
    operation: str
    argument: Any = None


@dataclass(frozen=True)
class ClientPlan:
    client_id: int
    operations: tuple[PlannedOperation, ...]
    seed: int
    yield_probability: float


@dataclass(frozen=True)
class WorkloadPlan:
    clients: tuple[ClientPlan, ...]
    producers: int
    consumers: int
    ops_per_client: int


@dataclass(frozen=True)
class OperationSpec:
    name: str
    invoke: Callable[[Any, Any], Any]
    input_for: Callable[[Any], dict[str, Any]]
    output_for: Callable[[Any], dict[str, Any]]


@dataclass(frozen=True)
class TraceEvent:
    client_id: int
    op_index: int
    operation: str
    input: dict[str, Any]
    output: dict[str, Any]
    call_ns: int
    return_ns: int

    def to_porcupine(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "input": self.input,
            "output": self.output,
            "call": self.call_ns,
            "return": self.return_ns,
        }


@dataclass(frozen=True)
class TraceCollection:
    events: tuple[TraceEvent, ...]
    errors: tuple[str, ...]
    timed_out: bool
    producers: int
    consumers: int

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.errors


def _invoke_enqueue(queue: Any, item: Any) -> Any:
    return queue.enqueue(item)


def _invoke_dequeue(queue: Any, _argument: Any) -> Any:
    return queue.dequeue()


def _enqueue_input(item: Any) -> dict[str, Any]:
    return {"kind": "enqueue", "value": item}


def _dequeue_input(_argument: Any) -> dict[str, Any]:
    return {"kind": "dequeue"}


def _enqueue_output(result: Any) -> dict[str, Any]:
    if not isinstance(result, bool):
        raise TypeError(f"enqueue must return bool, got {type(result).__name__}")
    return {"enqueue_ok": result}


def _dequeue_output(result: Any) -> dict[str, Any]:
    if result is None:
        return {"dequeue_none": True}
    if isinstance(result, int) and not isinstance(result, bool):
        return {"dequeue_none": False, "dequeue_value": result}
    raise TypeError(f"dequeue must return int|None, got {type(result).__name__}")


FIFO_OPERATION_SPECS: dict[str, OperationSpec] = {
    "enqueue": OperationSpec(
        name="enqueue",
        invoke=_invoke_enqueue,
        input_for=_enqueue_input,
        output_for=_enqueue_output,
    ),
    "dequeue": OperationSpec(
        name="dequeue",
        invoke=_invoke_dequeue,
        input_for=_dequeue_input,
        output_for=_dequeue_output,
    ),
}


def linearizable_worker_counts(
    contract: QueueContract,
    *,
    producers: int,
    consumers: int,
) -> tuple[int, int]:
    if not contract.configurable_producers:
        producers = contract.default_producers
    if not contract.configurable_consumers:
        consumers = contract.default_consumers
    if producers <= 0 or consumers <= 0:
        raise ValueError("linearizability traces require at least one producer and one consumer")
    return producers, consumers


def make_linearizability_workload(
    contract: QueueContract,
    *,
    ops: int,
    producers: int,
    consumers: int,
    seed: int,
) -> WorkloadPlan:
    producers, consumers = linearizable_worker_counts(
        contract,
        producers=producers,
        consumers=consumers,
    )
    total_clients = producers + consumers
    ops_per_client = max(1, ops // total_clients)
    rng = random.Random(seed)
    thread_seeds = [rng.randrange(1 << 30) for _ in range(total_clients)]
    clients: list[ClientPlan] = []

    for producer_id in range(producers):
        item_base = producer_id * ops_per_client
        operations = tuple(
            PlannedOperation("enqueue", item_base + op_index) for op_index in range(ops_per_client)
        )
        clients.append(
            ClientPlan(
                client_id=producer_id,
                operations=operations,
                seed=thread_seeds[producer_id],
                yield_probability=0.05,
            )
        )

    for consumer_offset in range(consumers):
        client_id = producers + consumer_offset
        operations = tuple(PlannedOperation("dequeue") for _ in range(ops_per_client))
        clients.append(
            ClientPlan(
                client_id=client_id,
                operations=operations,
                seed=thread_seeds[client_id],
                yield_probability=0.10,
            )
        )

    return WorkloadPlan(
        clients=tuple(clients),
        producers=producers,
        consumers=consumers,
        ops_per_client=ops_per_client,
    )


def collect_trace(
    target: Any,
    plan: WorkloadPlan,
    *,
    operation_specs: Mapping[str, OperationSpec] = FIFO_OPERATION_SPECS,
    timeout_seconds: float = 10.0,
) -> TraceCollection:
    barrier = threading.Barrier(len(plan.clients))
    per_client_events: list[list[TraceEvent]] = [[] for _ in plan.clients]
    errors: list[str] = []
    errors_lock = threading.Lock()

    def worker(index: int, client: ClientPlan) -> None:
        local_events: list[TraceEvent] = []
        rng = random.Random(client.seed)
        try:
            barrier.wait(timeout=timeout_seconds)
            for op_index, planned in enumerate(client.operations):
                if client.yield_probability and rng.random() < client.yield_probability:
                    time.sleep(0)
                spec = operation_specs[planned.operation]
                call_ns = time.monotonic_ns()
                raw_output = spec.invoke(target, planned.argument)
                return_ns = time.monotonic_ns()
                local_events.append(
                    TraceEvent(
                        client_id=client.client_id,
                        op_index=op_index,
                        operation=planned.operation,
                        input=spec.input_for(planned.argument),
                        output=spec.output_for(raw_output),
                        call_ns=call_ns,
                        return_ns=return_ns,
                    )
                )
        except Exception as exc:
            with errors_lock:
                errors.append(f"client {client.client_id}: {exc}")
        finally:
            per_client_events[index] = local_events

    threads = [
        threading.Thread(target=worker, args=(index, client), daemon=True)
        for index, client in enumerate(plan.clients)
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)

    events = [event for client_events in per_client_events for event in client_events]
    events.sort(key=lambda event: (event.call_ns, event.return_ns, event.client_id, event.op_index))
    return TraceCollection(
        events=tuple(events),
        errors=tuple(errors),
        timed_out=any(thread.is_alive() for thread in threads),
        producers=plan.producers,
        consumers=plan.consumers,
    )


def to_porcupine_history(events: Iterable[TraceEvent]) -> list[dict[str, Any]]:
    return [event.to_porcupine() for event in events]
