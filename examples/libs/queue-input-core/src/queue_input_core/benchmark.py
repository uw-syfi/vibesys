from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from queue_input_core.config import QueueInputConfig
from queue_input_core.contract import QueueContract


@dataclass(frozen=True)
class BenchmarkConfig:
    capacity: int
    item_bytes: int
    producers: int
    consumers: int
    duration: float
    warmup: float


@dataclass(frozen=True)
class BenchmarkResult:
    scenario: str
    enqueued: int
    dropped: int
    dequeued: int
    duration: float
    total_ops_per_sec: float
    producers: int
    consumers: int

    def to_dict(self) -> dict:
        return asdict(self)


def default_benchmark_config(
    contract: QueueContract | None,
    input_config: QueueInputConfig | None = None,
) -> BenchmarkConfig:
    return BenchmarkConfig(
        capacity=input_config.capacity
        if input_config and input_config.capacity is not None
        else (contract.default_capacity if contract else 1024),
        item_bytes=64,
        producers=input_config.producers
        if input_config and input_config.producers is not None
        else (contract.default_producers if contract else 1),
        consumers=input_config.consumers
        if input_config and input_config.consumers is not None
        else (contract.default_consumers if contract else 1),
        duration=10.0,
        warmup=2.0,
    )


def add_benchmark_arguments(
    parser: argparse.ArgumentParser,
    contract: QueueContract | None,
    input_config: QueueInputConfig | None = None,
) -> None:
    defaults = default_benchmark_config(contract, input_config)
    parser.add_argument("--capacity", type=int, default=defaults.capacity)
    parser.add_argument("--item-bytes", type=int, default=defaults.item_bytes)
    parser.add_argument("--producers", type=int, default=defaults.producers)
    parser.add_argument("--consumers", type=int, default=defaults.consumers)
    parser.add_argument("--duration", type=float, default=defaults.duration)
    parser.add_argument("--warmup", type=float, default=defaults.warmup)


def benchmark_config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        capacity=args.capacity,
        item_bytes=args.item_bytes,
        producers=args.producers,
        consumers=args.consumers,
        duration=args.duration,
        warmup=args.warmup,
    )


def make_queue(queue_cls, contract: QueueContract, capacity: int):
    return queue_cls(scenario=contract.name, capacity=capacity)


def _producer(queue, item, stop, c, lock):
    enc, drp = 0, 0
    while not stop.is_set():
        if queue.enqueue(item):
            enc += 1
        else:
            drp += 1
    with lock:
        c[0] += enc
        c[1] += drp


def _consumer(queue, stop, c, lock, is_batch):
    dec = 0
    while not stop.is_set() or queue.size() > 0:
        r = queue.dequeue()
        dec += len(r) if is_batch else (1 if r is not None else 0)
    with lock:
        c[0] += dec


def _worker_counts(contract: QueueContract, config: BenchmarkConfig) -> tuple[int, int]:
    producers = config.producers if contract.configurable_producers else contract.default_producers
    consumers = config.consumers if contract.configurable_consumers else contract.default_consumers
    return producers, consumers


def run_benchmark(queue, contract: QueueContract, config: BenchmarkConfig) -> BenchmarkResult:
    producers, consumers = _worker_counts(contract, config)
    is_batch = contract.batched_dequeue
    item = b"x" * config.item_bytes
    stop = threading.Event()
    lock = threading.Lock()
    pc, dc = [0, 0], [0]

    def make():
        ts = [
            threading.Thread(target=_producer, args=(queue, item, stop, pc, lock), daemon=True)
            for _ in range(producers)
        ]
        ts += [
            threading.Thread(target=_consumer, args=(queue, stop, dc, lock, is_batch), daemon=True)
            for _ in range(consumers)
        ]
        return ts

    if config.warmup > 0:
        wts = make()
        for t in wts:
            t.start()
        time.sleep(config.warmup)
        stop.set()
        for t in wts:
            t.join(timeout=2)
        stop.clear()
        pc[:] = [0, 0]
        dc[:] = [0]

    ts = make()
    for t in ts:
        t.start()
    t0 = time.perf_counter()
    time.sleep(config.duration)
    stop.set()
    for t in ts:
        t.join(timeout=5)
    elapsed = time.perf_counter() - t0
    enc, drp, dec = pc[0], pc[1], dc[0]
    return BenchmarkResult(
        scenario=contract.name,
        enqueued=enc,
        dropped=drp,
        dequeued=dec,
        duration=elapsed,
        total_ops_per_sec=(enc + dec) / elapsed,
        producers=producers,
        consumers=consumers,
    )


def print_benchmark_result(result: BenchmarkResult) -> None:
    print(
        f"Scenario: {result.scenario.upper()}  Duration: {result.duration:.1f}s  "
        f"Prod: {result.producers}  Cons: {result.consumers}"
    )
    print(
        f"  Enqueued: {result.enqueued:,} ({result.enqueued / result.duration:,.0f} ops/s)  "
        f"Dropped: {result.dropped:,}  "
        f"Dequeued: {result.dequeued:,} ({result.dequeued / result.duration:,.0f} ops/s)"
    )
    total = result.enqueued + result.dequeued
    print(f"  Total: {total:,} ({result.total_ops_per_sec:,.0f} ops/s)")


def write_benchmark_results(results: list[BenchmarkResult], output_json: str | Path) -> None:
    output_path = Path(output_json)
    output_path.write_text(json.dumps([result.to_dict() for result in results], indent=2))
    print(f"Results written to {output_path}")
