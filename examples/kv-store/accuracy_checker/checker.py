"""Accuracy checker: compare candidate KV server against Redis oracle.

Expects the candidate server to be already running. Starts its own Redis
oracle internally, runs deterministic ops against both, diffs responses.

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

import redis


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _find_redis():
    for p in [shutil.which("redis-server"), "/opt/homebrew/opt/redis/bin/redis-server", "/usr/bin/redis-server"]:
        if p and os.path.isfile(p):
            return p
    sys.exit("ERROR: redis-server not found")


def main():
    parser = argparse.ArgumentParser(description="KV store accuracy checker")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (must be already running)")
    parser.add_argument("--num-ops", type=int, default=5000)
    parser.add_argument("--num-keys", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    assert _wait(args.port, timeout=5), f"Candidate not responding on port {args.port}"

    redis_port = _free_port()
    redis_proc = subprocess.Popen(
        [_find_redis(), "--port", str(redis_port), "--loglevel", "warning", "--appendonly", "no", "--save", ""],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    atexit.register(lambda: (redis_proc.terminate(), redis_proc.wait()))
    assert _wait(redis_port), f"Redis oracle failed to start on {redis_port}"

    oracle = redis.Redis(host="127.0.0.1", port=redis_port, decode_responses=True, protocol=2)
    candidate = redis.Redis(host="127.0.0.1", port=args.port, decode_responses=True, protocol=2)
    oracle.flushdb()
    candidate.flushdb()

    rng = random.Random(args.seed)
    mismatches = 0

    for i in range(args.num_ops):
        op = rng.choices(["SET", "GET", "DEL", "HSET", "HGETALL"], weights=[30, 35, 5, 20, 10])[0]
        key = f"key:{rng.randint(0, args.num_keys - 1):06d}"

        if op == "SET":
            val = f"v:{rng.randint(0, 999999)}"
            o, c = oracle.set(key, val), candidate.set(key, val)
        elif op == "GET":
            o, c = oracle.get(key), candidate.get(key)
        elif op == "DEL":
            o, c = oracle.delete(key), candidate.delete(key)
        elif op == "HSET":
            fields = {f"f{j}": f"{rng.randint(0, 9999)}" for j in range(rng.randint(1, 4))}
            o, c = oracle.hset(f"h:{key}", mapping=fields), candidate.hset(f"h:{key}", mapping=fields)
        elif op == "HGETALL":
            o, c = oracle.hgetall(f"h:{key}"), candidate.hgetall(f"h:{key}")

        if o != c:
            mismatches += 1
            if mismatches <= 10:
                print(f"  MISMATCH op[{i}] {op} {key}: oracle={o} candidate={c}")

    print(f"\nOperations: {args.num_ops} | Mismatches: {mismatches}")
    if mismatches == 0:
        print("ALL CHECKS PASSED")
    else:
        print("ACCURACY CHECK FAILED")
    sys.exit(0 if mismatches == 0 else 1)


if __name__ == "__main__":
    main()
