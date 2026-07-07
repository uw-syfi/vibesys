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
from reference import KVStoreFactory


def _load_candidate():
    try:
        from main import VibeServeKVStore

        return VibeServeKVStore
    except ImportError as exc:
        raise RuntimeError("Could not import VibeServeKVStore from main.py") from exc


def _run_put(store, key, value):
    call = time.monotonic_ns()
    result = store.put(key, value)
    ret = time.monotonic_ns()
    if not isinstance(result, bool):
        raise TypeError(f"put must return bool, got {type(result).__name__}")
    return {"success": result}, call, ret


def _run_get(store, key):
    call = time.monotonic_ns()
    result = store.get(key)
    ret = time.monotonic_ns()
    if result is not None and (not isinstance(result, int) or isinstance(result, bool)):
        raise TypeError(f"get must return int|None, got {type(result).__name__}")
    return {"value": result}, call, ret


def _run_delete(store, key):
    call = time.monotonic_ns()
    result = store.delete(key)
    ret = time.monotonic_ns()
    if not isinstance(result, bool):
        raise TypeError(f"delete must return bool, got {type(result).__name__}")
    return {"success": result}, call, ret


def _run_porcupine_checker(history):
    checker_dir = Path(__file__).parent / "porcupine_checker"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(history, tmp)
        history_path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["go", "run", ".", "--history", str(history_path)],
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


def _collect_history(store, clients, ops, key_space, read_ratio, seed):
    if clients <= 0:
        raise ValueError("clients must be > 0")

    ops_per_client = max(1, ops // clients)
    history = []
    history_lock = threading.Lock()
    barrier = threading.Barrier(clients)

    def worker(client_id):
        rng = random.Random(seed + client_id)
        barrier.wait()
        for _ in range(ops_per_client):
            key = f"k{rng.randrange(key_space)}"
            action = rng.random()
            if action < read_ratio:
                output, call, ret = _run_get(store, key)
                inp = {"kind": "get", "key": key}
            elif action < (read_ratio + (1.0 - read_ratio) / 2):
                value = rng.randrange(1_000_000)
                output, call, ret = _run_put(store, key, value)
                inp = {"kind": "put", "key": key, "value": value}
            else:
                output, call, ret = _run_delete(store, key)
                inp = {"kind": "delete", "key": key}
            with history_lock:
                history.append(
                    {
                        "client_id": client_id,
                        "input": inp,
                        "output": output,
                        "call": call,
                        "return": ret,
                    }
                )

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    if any(t.is_alive() for t in threads):
        raise RuntimeError("timed out while collecting operation history")

    return history


def main():
    parser = argparse.ArgumentParser(description="Correctness checker for VibeServe KV store.")
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--ops", type=int, default=3000)
    parser.add_argument("--key-space", type=int, default=16)
    parser.add_argument("--read-ratio", type=float, default=0.6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--use-reference", action="store_true")
    args = parser.parse_args()

    if args.read_ratio < 0 or args.read_ratio > 1:
        raise ValueError("--read-ratio must be in [0, 1]")

    if args.use_reference:
        store = KVStoreFactory()
    else:
        cls = _load_candidate()
        store = cls()

    print("Collecting concurrent history ...")
    history = _collect_history(
        store,
        clients=args.clients,
        ops=args.ops,
        key_space=args.key_space,
        read_ratio=args.read_ratio,
        seed=args.seed,
    )
    print(f"  Collected {len(history)} operations")

    print("Running Porcupine linearizability check ...")
    _run_porcupine_checker(history)
    print("  PASS - history is linearizable")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL - {exc}")
        sys.exit(1)
