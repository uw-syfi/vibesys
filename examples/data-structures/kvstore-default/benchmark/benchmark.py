from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
from reference import KVStoreFactory


def _load_candidate():
    try:
        from main import VibeServeKVStore

        return VibeServeKVStore
    except ImportError:
        return None


def _worker(store, stop, lock, counters, key_space, read_ratio, seed):
    rng = random.Random(seed)
    while not stop.is_set():
        key = f"k{rng.randrange(key_space)}"
        r = rng.random()
        if r < read_ratio:
            store.get(key)
            with lock:
                counters["get"] += 1
        elif r < (read_ratio + (1.0 - read_ratio) / 2):
            store.put(key, rng.randrange(1_000_000))
            with lock:
                counters["put"] += 1
        else:
            store.delete(key)
            with lock:
                counters["delete"] += 1


def _run(store, clients, duration, warmup, key_space, read_ratio, seed):
    stop = threading.Event()
    lock = threading.Lock()
    counters = {"put": 0, "get": 0, "delete": 0}
    threads = [
        threading.Thread(
            target=_worker,
            args=(store, stop, lock, counters, key_space, read_ratio, seed + i),
            daemon=True,
        )
        for i in range(clients)
    ]

    if warmup > 0:
        for t in threads:
            t.start()
        time.sleep(warmup)
        stop.set()
        for t in threads:
            t.join(timeout=2)
        stop.clear()
        counters = {"put": 0, "get": 0, "delete": 0}
        threads = [
            threading.Thread(
                target=_worker,
                args=(store, stop, lock, counters, key_space, read_ratio, seed + i + clients),
                daemon=True,
            )
            for i in range(clients)
        ]

    for t in threads:
        t.start()

    t0 = time.perf_counter()
    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.perf_counter() - t0

    total = counters["put"] + counters["get"] + counters["delete"]
    print(f"Duration: {elapsed:.1f}s  Clients: {clients}")
    print(f"  Ops: put={counters['put']:,} get={counters['get']:,} delete={counters['delete']:,}")
    print(f"  Total: {total:,} ({total / elapsed:,.0f} ops/s)")

    return {
        "duration": elapsed,
        "clients": clients,
        "put": counters["put"],
        "get": counters["get"],
        "delete": counters["delete"],
        "total_ops_per_sec": total / elapsed,
    }


def main():
    parser = argparse.ArgumentParser(description="Throughput benchmark for a KV store.")
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--key-space", type=int, default=16)
    parser.add_argument("--read-ratio", type=float, default=0.6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--use-reference", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    if args.use_reference:
        store = KVStoreFactory()
    else:
        cls = _load_candidate()
        store = cls() if cls else KVStoreFactory()

    result = _run(
        store,
        args.clients,
        args.duration,
        args.warmup,
        args.key_space,
        args.read_ratio,
        args.seed,
    )

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results written to {args.output_json}")


if __name__ == "__main__":
    main()
