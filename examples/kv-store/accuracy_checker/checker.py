"""Correctness checker for the RESP2 KV server. Two phases, both must pass:

1. **Protocol robustness** (deterministic): a pipeline of commands with
   binary-hostile values (empty, embedded CRLF/NUL, a large value) is sent
   many-per-packet and read back, asserting exact replies. This catches the
   two bugs the linearizability phase can't reach — a length-ignoring /
   CRLF-splitting RESP parser, and broken multi-command-per-`recv` handling —
   which are exactly the failure modes a hand-rolled parser + pipelining risk.
   Also exercises HMSET (distinct ``+OK`` reply, not an int).

2. **Linearizability** (concurrent): several redis clients hammer the store,
   a timed operation history is recorded and verified linearizable with
   Porcupine (the Go model in ``porcupine_checker/``). Catches concurrency
   bugs — lost updates, torn reads, races under sharded locks — which is what
   the concurrent benchmark rewards. A single run is a strong probe, not a
   proof: a race surfaces only on interleavings that happen to occur.

Requires: ``redis`` (client), and Go on PATH to run the Porcupine model.
No redis-server needed — the Porcupine model *is* the spec.

Usage:
    python checker.py --port 6380
    python checker.py --port 6380 --clients 8 --ops 4000 --key-space 32
"""

import argparse
import json
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import redis

CHECKER_DIR = Path(__file__).resolve().parent / "porcupine_checker"


def _wait(port, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _record(history, lock, client_id, inp, output, call, ret):
    with lock:
        history.append(
            {"client_id": client_id, "input": inp, "output": output, "call": call, "return": ret}
        )


def _collect_history(port, clients, ops, key_space, read_ratio, seed):
    """Run `ops` operations spread across `clients` threads; return the history."""
    ops_per_client = max(1, ops // clients)
    history = []
    lock = threading.Lock()
    barrier = threading.Barrier(clients)
    errors = []

    def worker(client_id):
        r = redis.Redis(host="127.0.0.1", port=port, decode_responses=True, protocol=2)
        rng = random.Random(seed + client_id)
        try:
            barrier.wait()
            for _ in range(ops_per_client):
                # Two disjoint key spaces so a key is only ever a string or a hash.
                action = rng.random()
                if rng.random() < 0.5:  # string key
                    key = f"s:{rng.randrange(key_space):05d}"
                    if action < read_ratio:
                        call = time.monotonic_ns()
                        v = r.get(key)
                        _record(history, lock, client_id, {"kind": "get", "key": key},
                                {"value": v}, call, time.monotonic_ns())
                    elif action < read_ratio + (1 - read_ratio) * 0.8:
                        val = f"v:{rng.randrange(1_000_000)}"
                        call = time.monotonic_ns()
                        ok = r.set(key, val)
                        _record(history, lock, client_id, {"kind": "set", "key": key, "value": val},
                                {"ok": bool(ok)}, call, time.monotonic_ns())
                    else:
                        call = time.monotonic_ns()
                        existed = r.delete(key)
                        _record(history, lock, client_id, {"kind": "del", "key": key},
                                {"existed": int(existed)}, call, time.monotonic_ns())
                else:  # hash key
                    key = f"h:{rng.randrange(key_space):05d}"
                    if action < read_ratio:
                        call = time.monotonic_ns()
                        h = r.hgetall(key)
                        _record(history, lock, client_id, {"kind": "hgetall", "key": key},
                                {"hash": h}, call, time.monotonic_ns())
                    else:
                        fields = {f"f{j}": str(rng.randrange(10_000)) for j in range(rng.randint(1, 4))}
                        call = time.monotonic_ns()
                        added = r.hset(key, mapping=fields)
                        _record(history, lock, client_id, {"kind": "hset", "key": key, "fields": fields},
                                {"added": int(added)}, call, time.monotonic_ns())
        except redis.RedisError as exc:
            errors.append(f"client {client_id}: candidate raised {exc!r}")
        finally:
            r.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    if any(t.is_alive() for t in threads):
        raise RuntimeError("timed out collecting history (candidate too slow or hung)")
    if errors:
        raise RuntimeError("; ".join(errors))
    return history


def _check_linearizable(history):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(history, tmp)
        path = Path(tmp.name)
    try:
        proc = subprocess.run(
            ["go", "run", ".", "--history", str(path)],
            cwd=CHECKER_DIR, capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Go is required on PATH to run the Porcupine checker") from exc
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "Porcupine check failed")


# Binary-hostile values: a length-honest RESP2 parser round-trips all of these;
# one that splits on CRLF or ignores the bulk length corrupts them.
_ADVERSARIAL = {
    "p:empty": "",
    "p:crlf": "a\r\nb\r\n",
    "p:nul": "x\x00y",
    "p:big": "z" * 20000,
    "p:plain": "hello",
}


def _check_protocol(port):
    """Deterministic framing + pipelining + HMSET checks with exact-reply asserts."""
    r = redis.Redis(host="127.0.0.1", port=port, decode_responses=True, protocol=2)
    errs = []
    try:
        # Write everything as ONE pipeline (many commands per send).
        load = r.pipeline(transaction=False)
        for k, v in _ADVERSARIAL.items():
            load.set(k, v)
        load.hset("p:hash", mapping={"a": "1", "b": "2\r\n3"})
        load.execute()

        # HMSET is in the required subset and must reply +OK, not an int count.
        hmset_reply = r.execute_command("HMSET", "p:hash2", "f", "v\r\nw", "g", "")
        if hmset_reply not in (True, "OK", b"OK"):
            errs.append(f"HMSET replied {hmset_reply!r}, expected +OK")

        # Read back in one pipeline; assert every reply byte-exact.
        read = r.pipeline(transaction=False)
        for k in _ADVERSARIAL:
            read.get(k)
        read.hgetall("p:hash")
        read.hgetall("p:hash2")
        got = read.execute()

        expected = list(_ADVERSARIAL.values()) + [
            {"a": "1", "b": "2\r\n3"},
            {"f": "v\r\nw", "g": ""},
        ]
        keys = list(_ADVERSARIAL) + ["p:hash", "p:hash2"]
        for key, exp, act in zip(keys, expected, got, strict=True):
            if act != exp:
                errs.append(f"{key}: expected {exp!r}, got {act!r}")
    except redis.RedisError as exc:
        raise RuntimeError(f"protocol check: candidate raised {exc!r}") from exc
    finally:
        r.close()
    if errs:
        raise RuntimeError("protocol/framing mismatch: " + "; ".join(errs))


def main():
    parser = argparse.ArgumentParser(description="KV store linearizability checker")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (already running)")
    parser.add_argument("--clients", type=int, default=4, help="Concurrent client threads")
    parser.add_argument("--ops", type=int, default=2000, help="Total operations across all clients")
    parser.add_argument("--key-space", type=int, default=32, help="Distinct keys per type (higher = less contention)")
    parser.add_argument("--read-ratio", type=float, default=0.5, help="Fraction of ops that are reads")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not _wait(args.port):
        sys.exit(f"FAIL - candidate not responding on port {args.port}")

    redis.Redis(host="127.0.0.1", port=args.port, decode_responses=True, protocol=2).flushdb()

    print("Checking protocol robustness (framing, pipelining, HMSET) ...")
    _check_protocol(args.port)
    print("  OK")

    print(f"Collecting concurrent history ({args.clients} clients, ~{args.ops} ops) ...")
    history = _collect_history(args.port, args.clients, args.ops, args.key_space, args.read_ratio, args.seed)
    print(f"  Collected {len(history)} operations")

    print("Running Porcupine linearizability check ...")
    _check_linearizable(history)
    print("ALL CHECKS PASSED - history is linearizable")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ACCURACY CHECK FAILED - {exc}")
        sys.exit(1)
