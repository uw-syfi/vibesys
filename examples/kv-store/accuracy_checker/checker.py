"""Accuracy checker: compare a candidate KV server against a Redis oracle.

The candidate server must already be running. This starts its own Redis oracle
and runs two correctness phases against both, diffing every reply:

  1. Sequential — a deterministic single-connection operation stream, checked in
     lock-step (per-op semantics).
  2. Concurrent — many client threads under load, since the objective rewards
     server-side concurrency (lock sharding, SO_REUSEPORT, multi-process). Each
     thread drives a disjoint key namespace, so the expected final state stays
     deterministic despite concurrency; a final reconciliation reads every key
     back over several fresh connections to expose split-brain (e.g. per-process
     unshared maps behind SO_REUSEPORT) and lost concurrent writes.

Usage:
    python checker.py --port 6380
    python checker.py --port 6380 --num-ops 10000
    python checker.py --port 6380 --threads 32 --concurrent-ops 40000
    python checker.py --port 6380 --no-concurrent
"""

import argparse
import atexit
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

import redis

# One replayed request: the redis-py method to call and its arguments.
Operation = namedtuple("Operation", ["label", "method", "args", "kwargs"])


def _client(port):
    return redis.Redis(host="127.0.0.1", port=port, decode_responses=True, protocol=2)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_listening(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _find_redis_server():
    candidates = [shutil.which("redis-server"), "/opt/homebrew/opt/redis/bin/redis-server", "/usr/bin/redis-server"]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    sys.exit("ERROR: redis-server not found")


def _next_operation(rng, num_keys, prefix=""):
    """Draw the next random operation from the workload mix. `prefix` gives each
    concurrent thread a disjoint key namespace so its expected state is
    deterministic even under load."""
    kind = rng.choices(["SET", "GET", "DEL", "HSET", "HGETALL"], weights=[30, 35, 5, 20, 10])[0]
    key = f"{prefix}key:{rng.randint(0, num_keys - 1):06d}"

    if kind == "SET":
        return Operation(kind, "set", (key, f"v:{rng.randint(0, 999999)}"), {})
    if kind == "GET":
        return Operation(kind, "get", (key,), {})
    if kind == "DEL":
        return Operation(kind, "delete", (key,), {})
    if kind == "HSET":
        fields = {f"f{i}": f"{rng.randint(0, 9999)}" for i in range(rng.randint(1, 4))}
        return Operation(kind, "hset", (f"h:{key}",), {"mapping": fields})
    return Operation(kind, "hgetall", (f"h:{key}",), {})


def _start_oracle():
    """Launch a throwaway Redis oracle and return (client, port)."""
    port = _free_port()
    process = subprocess.Popen(
        [_find_redis_server(), "--port", str(port), "--loglevel", "warning", "--appendonly", "no", "--save", ""],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    atexit.register(lambda: (process.terminate(), process.wait()))
    assert _wait_until_listening(port), f"Redis oracle failed to start on {port}"
    return _client(port), port


def _read_key(conn, method, key):
    """Read `key` from the candidate, turning a RESP/connection error into a
    comparable sentinel so a candidate failure counts as a mismatch instead of
    crashing the checker."""
    try:
        return getattr(conn, method)(key)
    except redis.RedisError as exc:
        return f"<error: {exc!r}>"


def _sequential_phase(oracle, candidate, num_ops, num_keys, seed):
    """Deterministic single-connection diff: run each op on oracle then candidate
    and compare in lock-step. Returns the mismatch count."""
    rng = random.Random(seed)
    mismatches = 0
    for i in range(num_ops):
        op = _next_operation(rng, num_keys)

        expected = getattr(oracle, op.method)(*op.args, **op.kwargs)
        try:
            actual = getattr(candidate, op.method)(*op.args, **op.kwargs)
        except redis.RedisError as exc:
            # Malformed RESP or a dropped connection is a candidate failure, not a
            # checker crash — count it and report cleanly instead of a traceback.
            mismatches += 1
            print(f"  ERROR op[{i}] {op.label} {op.args[0]}: candidate raised {exc!r}")
            if isinstance(exc, redis.ConnectionError):
                print("  candidate connection lost — aborting run")
                break
            continue

        if expected != actual:
            mismatches += 1
            if mismatches <= 10:
                print(f"  MISMATCH op[{i}] {op.label} {op.args[0]}: oracle={expected} candidate={actual}")
    return mismatches


def _stress_client(port, oracle_port, tid, num_ops, num_keys, seed):
    """One concurrent client: mirror a disjoint-namespace op-mix to both oracle
    and candidate. The thread-id prefix means no two threads ever touch the same
    key, so even the in-flight replies must agree. Returns the mismatch count."""
    oracle, candidate = _client(oracle_port), _client(port)
    rng = random.Random(seed + tid)
    prefix = f"t{tid}:"
    mismatches = 0
    for _ in range(num_ops):
        op = _next_operation(rng, num_keys, prefix)
        expected = getattr(oracle, op.method)(*op.args, **op.kwargs)
        try:
            actual = getattr(candidate, op.method)(*op.args, **op.kwargs)
        except redis.RedisError as exc:
            mismatches += 1
            if isinstance(exc, redis.ConnectionError):
                break  # this candidate connection is gone; stop hammering it
            continue
        if expected != actual:
            mismatches += 1
    return mismatches


def _fanin_client(port, oracle_port, tid, hash_keys):
    """Write a field unique to this thread into every shared hash, mirrored to
    the oracle. Distinct fields never conflict, so once all threads join each
    hash must hold every thread's field — a deterministic torture test for the
    concurrent HSET path that catches lost fields and split-brain. Returns the
    candidate error count."""
    oracle, candidate = _client(oracle_port), _client(port)
    field, value = f"f{tid}", f"v{tid}"
    errors = 0
    for hkey in hash_keys:
        oracle.hset(hkey, field, value)
        try:
            candidate.hset(hkey, field, value)
        except redis.RedisError:
            errors += 1
    return errors


def _reconcile(oracle, candidate_conns, keys, label):
    """Compare each key's final state between oracle and candidate, spreading the
    candidate reads round-robin over several fresh connections so they fan out
    across candidate worker processes — exposing SO_REUSEPORT split-brain where
    each process holds an unshared map. Returns the mismatch count."""
    mismatches = 0
    for i, key in enumerate(keys):
        conn = candidate_conns[i % len(candidate_conns)]
        if oracle.type(key) == "hash":
            expected, actual = oracle.hgetall(key), _read_key(conn, "hgetall", key)
        else:
            expected, actual = oracle.get(key), _read_key(conn, "get", key)
        if expected != actual:
            mismatches += 1
            if mismatches <= 10:
                print(f"  RECONCILE MISMATCH [{label}] {key}: oracle={expected} candidate={actual}")
    return mismatches


def _concurrent_phase(oracle, oracle_port, args):
    """Concurrent-load phase: disjoint-namespace stress + shared-hash fan-in, each
    followed by a final-state reconciliation over fresh connections. Runs on a
    clean db so the reconciliation scan only sees keys this phase wrote. Returns
    the total mismatch count."""
    oracle.flushdb()
    _client(args.port).flushdb()
    per_thread = max(1, args.concurrent_ops // args.threads)

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        inflight = sum(pool.map(
            lambda tid: _stress_client(args.port, oracle_port, tid, per_thread, args.num_keys, args.seed),
            range(args.threads),
        ))

    candidate_conns = [_client(args.port) for _ in range(args.verify_conns)]
    stress_keys = sorted(oracle.keys("*"))
    stress_recon = _reconcile(oracle, candidate_conns, stress_keys, "concurrent")

    hash_keys = [f"shared:h:{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        fanin_errors = sum(pool.map(
            lambda tid: _fanin_client(args.port, oracle_port, tid, hash_keys),
            range(args.threads),
        ))
    fanin = fanin_errors + _reconcile(oracle, candidate_conns, hash_keys, "shared-hash")

    print(f"  concurrent: threads={args.threads} ops={per_thread * args.threads} "
          f"inflight_mismatches={inflight} reconciled_keys={len(stress_keys)} "
          f"reconcile_mismatches={stress_recon}")
    print(f"  shared-hash: keys={len(hash_keys)} mismatches={fanin}")
    return inflight + stress_recon + fanin


def main():
    parser = argparse.ArgumentParser(description="KV store accuracy checker")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (must be already running)")
    parser.add_argument("--num-ops", type=int, default=5000, help="Sequential-phase op count.")
    parser.add_argument("--num-keys", type=int, default=200, help="Keyspace size per namespace.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=16,
                        help="Concurrent client threads for the concurrency phase (matches the benchmark headline).")
    parser.add_argument("--concurrent-ops", type=int, default=20000, help="Total ops across all concurrent threads.")
    parser.add_argument("--verify-conns", type=int, default=8,
                        help="Fresh candidate connections used to fan reconciliation reads across worker processes.")
    parser.add_argument("--no-concurrent", action="store_true", help="Run only the sequential phase.")
    args = parser.parse_args()

    assert _wait_until_listening(args.port, timeout=5), f"Candidate not responding on port {args.port}"

    oracle, oracle_port = _start_oracle()
    candidate = _client(args.port)
    oracle.flushdb()
    candidate.flushdb()

    print("=== SEQUENTIAL ===")
    mismatches = _sequential_phase(oracle, candidate, args.num_ops, args.num_keys, args.seed)
    print(f"  sequential: ops={args.num_ops} mismatches={mismatches}")

    if not args.no_concurrent:
        print("=== CONCURRENT ===")
        mismatches += _concurrent_phase(oracle, oracle_port, args)

    print(f"\nTotal mismatches: {mismatches}")
    print("ALL CHECKS PASSED" if mismatches == 0 else "ACCURACY CHECK FAILED")
    sys.exit(0 if mismatches == 0 else 1)


if __name__ == "__main__":
    main()
