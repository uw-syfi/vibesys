from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from queue_input_core.config import load_config
from queue_input_core.contract import SCENARIOS, get_contract
from queue_input_core.reference import QueueFactory


def _load_candidate():
    workspace = Path.cwd()
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    try:
        from main import VibeServeQueue

        return VibeServeQueue
    except ImportError:
        return None


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


def _run(queue, scenario, duration, warmup, producers, consumers, item_bytes):
    is_batch = scenario == "batch"
    item = b"x" * item_bytes
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

    if warmup > 0:
        wts = make()
        for t in wts:
            t.start()
        time.sleep(warmup)
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
    time.sleep(duration)
    stop.set()
    for t in ts:
        t.join(timeout=5)
    elapsed = time.perf_counter() - t0
    enc, drp, dec = pc[0], pc[1], dc[0]
    print(
        f"Scenario: {scenario.upper()}  Duration: {elapsed:.1f}s  Prod: {producers}  Cons: {consumers}"
    )
    print(
        f"  Enqueued: {enc:,} ({enc / elapsed:,.0f} ops/s)  Dropped: {drp:,}  Dequeued: {dec:,} ({dec / elapsed:,.0f} ops/s)"
    )
    print(f"  Total: {enc + dec:,} ({(enc + dec) / elapsed:,.0f} ops/s)")
    return {
        "scenario": scenario,
        "enqueued": enc,
        "dropped": drp,
        "dequeued": dec,
        "duration": elapsed,
        "total_ops_per_sec": (enc + dec) / elapsed,
    }


def main(default_scenario: str | None = None):
    config = load_config()
    scenario_default = default_scenario or config.scenario or "spsc"
    contract_default = get_contract(scenario_default) if scenario_default != "all" else None
    capacity_default = config.capacity or (
        contract_default.default_capacity if contract_default else 1024
    )
    producers_default = config.producers or (
        contract_default.default_producers if contract_default else 1
    )
    consumers_default = config.consumers or (
        contract_default.default_consumers if contract_default else 1
    )

    parser = argparse.ArgumentParser(
        description="Throughput benchmark for VibeServe queue scenarios."
    )
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default=scenario_default)
    parser.add_argument("--capacity", type=int, default=capacity_default)
    parser.add_argument("--item-bytes", type=int, default=64)
    parser.add_argument("--producers", type=int, default=producers_default)
    parser.add_argument("--consumers", type=int, default=consumers_default)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--use-reference", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()
    targets = SCENARIOS if args.scenario == "all" else [args.scenario]
    results = []
    for s in targets:
        contract = get_contract(s)
        n_prod = args.producers if contract.configurable_producers else contract.default_producers
        n_cons = args.consumers if contract.configurable_consumers else contract.default_consumers
        if args.use_reference:
            q = QueueFactory(s, args.capacity)
        else:
            cls = _load_candidate()
            q = cls(scenario=s, capacity=args.capacity) if cls else QueueFactory(s, args.capacity)
        results.append(_run(q, s, args.duration, args.warmup, n_prod, n_cons, args.item_bytes))
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results written to {args.output_json}")


if __name__ == "__main__":
    main()
