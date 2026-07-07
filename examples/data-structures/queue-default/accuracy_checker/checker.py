from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
from reference import SCENARIOS


def _load_candidate():
    try:
        from main import VibeServeQueue

        return VibeServeQueue
    except ImportError as exc:
        raise RuntimeError("Could not import VibeServeQueue from main.py") from exc


def _run_enqueue(queue, item):
    call = time.monotonic_ns()
    result = queue.enqueue(item)
    ret = time.monotonic_ns()
    if not isinstance(result, bool):
        raise TypeError(f"enqueue must return bool, got {type(result).__name__}")
    return {"enqueue_ok": result}, call, ret


def _run_dequeue(queue):
    call = time.monotonic_ns()
    result = queue.dequeue()
    ret = time.monotonic_ns()
    if result is None:
        output = {"dequeue_none": True}
    elif isinstance(result, int) and not isinstance(result, bool):
        output = {"dequeue_none": False, "dequeue_value": result}
    else:
        raise TypeError(f"dequeue must return int|None, got {type(result).__name__}")
    return output, call, ret


def _run_porcupine_checker(history, capacity):
    checker_dir = Path(__file__).parent / "porcupine_checker"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(history, tmp)
        history_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            [
                "go",
                "run",
                ".",
                "--history",
                str(history_path),
                "--capacity",
                str(capacity),
            ],
            cwd=checker_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Go runtime is required to run Porcupine checker") from exc
    finally:
        history_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(detail or "Porcupine checker failed")


def _check_linearizable_queue(cls, scenario, capacity, ops, producers, consumers, seed):
    if scenario == "spsc":
        producers, consumers = 1, 1
    elif scenario == "mpsc":
        consumers = 1

    queue = cls(scenario=scenario, capacity=capacity)
    total_clients = producers + consumers
    ops_per_client = max(1, ops // total_clients)
    history = []
    history_lock = threading.Lock()
    barrier = threading.Barrier(total_clients)
    next_item = 0
    next_item_lock = threading.Lock()
    rng = random.Random(seed)
    thread_seeds = [rng.randrange(1 << 30) for _ in range(total_clients)]

    def producer(client_id, local_seed):
        nonlocal next_item
        local_rng = random.Random(local_seed)
        barrier.wait()
        for _ in range(ops_per_client):
            with next_item_lock:
                item = next_item
                next_item += 1
            if local_rng.random() < 0.05:
                time.sleep(0)
            output, call, ret = _run_enqueue(queue, item)
            with history_lock:
                history.append(
                    {
                        "client_id": client_id,
                        "input": {"kind": "enqueue", "value": item},
                        "output": output,
                        "call": call,
                        "return": ret,
                    }
                )

    def consumer(client_id, local_seed):
        local_rng = random.Random(local_seed)
        barrier.wait()
        for _ in range(ops_per_client):
            if local_rng.random() < 0.1:
                time.sleep(0)
            output, call, ret = _run_dequeue(queue)
            with history_lock:
                history.append(
                    {
                        "client_id": client_id,
                        "input": {"kind": "dequeue"},
                        "output": output,
                        "call": call,
                        "return": ret,
                    }
                )

    threads = []
    for client_id in range(producers):
        threads.append(
            threading.Thread(
                target=producer,
                args=(client_id, thread_seeds[client_id]),
                daemon=True,
            )
        )
    for offset in range(consumers):
        client_id = producers + offset
        threads.append(
            threading.Thread(
                target=consumer,
                args=(client_id, thread_seeds[client_id]),
                daemon=True,
            )
        )

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    if any(thread.is_alive() for thread in threads):
        return False, f"{scenario.upper()} timed out while collecting history"

    try:
        _run_porcupine_checker(history, capacity)
    except Exception as exc:
        return False, f"{scenario.upper()} linearizability failure: {exc}"

    return (
        True,
        f"{scenario.upper()} linearizable ({len(history)} ops, {producers}P/{consumers}C, capacity={capacity})",
    )


def _check_lossy(cls, capacity, ops, seed):
    queue = cls(scenario="lossy", capacity=capacity)
    rng = random.Random(seed)
    enqueued, dequeued = set(), []
    for i in range(ops):
        if rng.random() < 0.6:
            ok = queue.enqueue(i)
            if not ok:
                return False, f"Lossy enqueue returned False at op {i}"
            enqueued.add(i)
        else:
            x = queue.dequeue()
            if x is not None:
                dequeued.append(x)
        if queue.size() > capacity:
            return False, f"size {queue.size()} > capacity {capacity}"
    fabricated = set(dequeued) - enqueued
    if fabricated:
        return False, f"Lossy returned items never enqueued: {list(fabricated)[:5]}"
    return True, f"Lossy OK ({ops} ops, capacity={capacity}, dequeued={len(dequeued)})"


def _check_batch(cls, capacity, ops, seed):
    queue = cls(scenario="batch", capacity=capacity)
    rng = random.Random(seed)
    enqueued, dequeued = [], []
    for i in range(ops):
        if rng.random() < 0.6:
            if queue.enqueue(i):
                enqueued.append(i)
        else:
            batch = queue.dequeue()
            if not isinstance(batch, list):
                return False, f"Batch dequeue must return list, got {type(batch).__name__}"
            dequeued.extend(batch)
    fabricated = set(dequeued) - set(enqueued)
    if fabricated:
        return False, f"Batch returned items never enqueued: {list(fabricated)[:5]}"
    duplicates = len(dequeued) - len(set(dequeued))
    if duplicates:
        return False, f"Batch returned {duplicates} duplicates"
    return True, f"Batch OK ({ops} ops, capacity={capacity}, dequeued={len(dequeued)})"


def main():
    parser = argparse.ArgumentParser(
        description="Correctness checker for VibeServe queue scenarios."
    )
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--capacity", type=int, default=64)
    parser.add_argument("--ops", type=int, default=2000)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--consumers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("Loading VibeServeQueue from main.py ...")
    cls = _load_candidate()
    print("  Loaded.")

    targets = SCENARIOS if args.scenario == "all" else [args.scenario]
    results = {}
    for scenario in targets:
        print(f"[{scenario.upper()}] Checking ...")
        try:
            if scenario in {"spsc", "mpsc", "mpmc"}:
                ok, msg = _check_linearizable_queue(
                    cls,
                    scenario,
                    args.capacity,
                    args.ops,
                    args.producers,
                    args.consumers,
                    args.seed,
                )
            elif scenario == "lossy":
                ok, msg = _check_lossy(cls, args.capacity, args.ops, args.seed)
            elif scenario == "batch":
                ok, msg = _check_batch(cls, args.capacity, args.ops, args.seed)
            else:
                ok, msg = False, f"Unknown scenario: {scenario}"
        except Exception as exc:
            ok, msg = False, f"Exception: {exc}"
        print(f"  PASS - {msg}" if ok else f"  FAIL - {msg}")
        results[scenario] = ok

    passed = sum(results.values())
    print(f"Results: {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
