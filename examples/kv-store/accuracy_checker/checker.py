"""Accuracy checker: compare a candidate KV server against a Redis oracle.

The candidate server must already be running. This starts its own Redis oracle,
replays a deterministic operation sequence against both, and diffs every reply.

Usage:
    python checker.py --port 6380
    python checker.py --port 6380 --num-ops 10000
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

import redis

# One replayed request: the redis-py method to call and its arguments.
Operation = namedtuple("Operation", ["label", "method", "args", "kwargs"])


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


def _next_operation(rng, num_keys):
    """Draw the next random operation from the workload mix."""
    kind = rng.choices(["SET", "GET", "DEL", "HSET", "HGETALL"], weights=[30, 35, 5, 20, 10])[0]
    key = f"key:{rng.randint(0, num_keys - 1):06d}"

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
    """Launch a throwaway Redis oracle and return a connected client."""
    port = _free_port()
    process = subprocess.Popen(
        [_find_redis_server(), "--port", str(port), "--loglevel", "warning", "--appendonly", "no", "--save", ""],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    atexit.register(lambda: (process.terminate(), process.wait()))
    assert _wait_until_listening(port), f"Redis oracle failed to start on {port}"
    return redis.Redis(host="127.0.0.1", port=port, decode_responses=True, protocol=2)


def main():
    parser = argparse.ArgumentParser(description="KV store accuracy checker")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (must be already running)")
    parser.add_argument("--num-ops", type=int, default=5000)
    parser.add_argument("--num-keys", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    assert _wait_until_listening(args.port, timeout=5), f"Candidate not responding on port {args.port}"

    oracle = _start_oracle()
    candidate = redis.Redis(host="127.0.0.1", port=args.port, decode_responses=True, protocol=2)
    oracle.flushdb()
    candidate.flushdb()

    rng = random.Random(args.seed)
    mismatches = 0

    for i in range(args.num_ops):
        op = _next_operation(rng, args.num_keys)

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

    print(f"\nOperations: {args.num_ops} | Mismatches: {mismatches}")
    print("ALL CHECKS PASSED" if mismatches == 0 else "ACCURACY CHECK FAILED")
    sys.exit(0 if mismatches == 0 else 1)


if __name__ == "__main__":
    main()
