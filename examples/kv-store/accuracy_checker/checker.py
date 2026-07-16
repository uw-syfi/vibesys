"""Accuracy checker: compare a candidate KV server against a Redis oracle.

By default the checker launches ``./run.sh <port>`` and always cleans it up;
``--port`` targets an already-running candidate. It starts its own Redis oracle
and runs three correctness phases:

  1. Semantics/RESP2 — required commands, binary values, wrong types, fragmented
     frames, and pipelines.
  2. Sequential — a deterministic single-connection operation stream, checked in
     lock-step (per-op semantics).
  3. Concurrent — many client threads under load, since the objective rewards
     server-side concurrency (lock sharding, SO_REUSEPORT, multi-process). Each
     thread drives a disjoint key namespace, so the expected final state stays
     deterministic despite concurrency; a final reconciliation reads every key
     back over several fresh connections to expose split-brain (e.g. per-process
     unshared maps behind SO_REUSEPORT) and lost concurrent writes.

Usage:
    python checker.py
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
from pathlib import Path

import redis

_WORKSPACE = Path(__file__).resolve().parents[1]
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from evaluator_support import candidate_server  # noqa: E402

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
    candidates = [
        shutil.which("redis-server"),
        "/opt/homebrew/opt/redis/bin/redis-server",
        "/usr/bin/redis-server",
    ]
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
        [
            _find_redis_server(),
            "--port",
            str(port),
            "--loglevel",
            "warning",
            "--appendonly",
            "no",
            "--save",
            "",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    atexit.register(lambda: (process.terminate(), process.wait()))
    assert _wait_until_listening(port), f"Redis oracle failed to start on {port}"
    return _client(port), port


def _compare_call(label, oracle_call, candidate_call):
    """Compare one semantic operation, including matching Redis errors."""
    try:
        expected = oracle_call()
    except redis.ResponseError as exc:
        expected = ("error", str(exc).split(" ", 1)[0])
    try:
        actual = candidate_call()
    except redis.ResponseError as exc:
        actual = ("error", str(exc).split(" ", 1)[0])
    except redis.RedisError as exc:
        print(f"  SEMANTIC ERROR [{label}]: candidate raised {exc!r}")
        return 1
    if expected != actual:
        print(f"  SEMANTIC MISMATCH [{label}]: oracle={expected!r} candidate={actual!r}")
        return 1
    return 0


def _raw_round_trip(port, chunks):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        for chunk in chunks:
            sock.sendall(chunk)
            time.sleep(0.005)
        sock.shutdown(socket.SHUT_WR)
        reply = bytearray()
        while True:
            try:
                part = sock.recv(4096)
            except TimeoutError:
                break
            if not part:
                break
            reply.extend(part)
        return bytes(reply)


def _semantic_phase(oracle, candidate, port):
    """Deterministic command, type, binary, framing, and pipeline coverage."""
    oracle.flushdb()
    candidate.flushdb()
    mismatches = 0

    cases = [
        ("PING", oracle.ping, candidate.ping),
        ("SET", lambda: oracle.set("k", "v1"), lambda: candidate.set("k", "v1")),
        ("GET", lambda: oracle.get("k"), lambda: candidate.get("k")),
        ("SET overwrite", lambda: oracle.set("k", "v2"), lambda: candidate.set("k", "v2")),
        ("GET overwrite", lambda: oracle.get("k"), lambda: candidate.get("k")),
        (
            "HSET new fields",
            lambda: oracle.hset("h", mapping={"a": "1", "b": "2"}),
            lambda: candidate.hset("h", mapping={"a": "1", "b": "2"}),
        ),
        (
            "HSET existing field",
            lambda: oracle.hset("h", "a", "3"),
            lambda: candidate.hset("h", "a", "3"),
        ),
        (
            "HMSET",
            lambda: oracle.execute_command("HMSET", "hm", "a", "1", "b", "2"),
            lambda: candidate.execute_command("HMSET", "hm", "a", "1", "b", "2"),
        ),
        ("HGETALL", lambda: oracle.hgetall("h"), lambda: candidate.hgetall("h")),
        ("DBSIZE", oracle.dbsize, candidate.dbsize),
        (
            "DEL multiple",
            lambda: oracle.delete("k", "h", "missing"),
            lambda: candidate.delete("k", "h", "missing"),
        ),
    ]
    for label, oracle_call, candidate_call in cases:
        mismatches += _compare_call(label, oracle_call, candidate_call)

    oracle.set("typed", "string")
    candidate.set("typed", "string")
    mismatches += _compare_call(
        "WRONGTYPE hash-on-string",
        lambda: oracle.hset("typed", "f", "v"),
        lambda: candidate.hset("typed", "f", "v"),
    )
    oracle.hset("typed-hash", "f", "v")
    candidate.hset("typed-hash", "f", "v")
    mismatches += _compare_call(
        "WRONGTYPE string-on-hash",
        lambda: oracle.get("typed-hash"),
        lambda: candidate.get("typed-hash"),
    )
    mismatches += _compare_call(
        "invalid SET arity",
        lambda: oracle.execute_command("SET", "typed"),
        lambda: candidate.execute_command("SET", "typed"),
    )
    mismatches += _compare_call(
        "invalid HSET arity",
        lambda: oracle.execute_command("HSET", "typed-hash", "field-only"),
        lambda: candidate.execute_command("HSET", "typed-hash", "field-only"),
    )
    if candidate.get("typed") != "string" or candidate.hgetall("typed-hash") != {"f": "v"}:
        print("  invalid-arity command mutated state")
        mismatches += 1

    oracle_raw = redis.Redis(
        host="127.0.0.1", port=oracle.connection_pool.connection_kwargs["port"]
    )
    candidate_raw = redis.Redis(host="127.0.0.1", port=port, protocol=2)
    binary_key, binary_value = b"\x00key\xff", b"\x00value\r\n\xff"
    mismatches += _compare_call(
        "binary SET",
        lambda: oracle_raw.set(binary_key, binary_value),
        lambda: candidate_raw.set(binary_key, binary_value),
    )
    mismatches += _compare_call(
        "binary GET",
        lambda: oracle_raw.get(binary_key),
        lambda: candidate_raw.get(binary_key),
    )

    for label, command in (
        ("COMMAND", ("COMMAND",)),
        ("CLIENT", ("CLIENT", "SETNAME", "vibeserve-checker")),
        ("HELLO", ("HELLO", "2")),
    ):
        try:
            candidate.execute_command(*command)
        except (redis.RedisError, IndexError, TypeError, ValueError) as exc:
            print(f"  SEMANTIC ERROR [{label} compatibility]: {exc!r}")
            mismatches += 1

    oracle.flushdb()
    candidate.flushdb()
    request = b"*3\r\n$3\r\nSET\r\n$4\r\nfrag\r\n$5\r\nvalue\r\n"
    pipeline = b"*2\r\n$3\r\nGET\r\n$4\r\nfrag\r\n*1\r\n$6\r\nDBSIZE\r\n*1\r\n$3\r\nGET\r\n"
    reply = _raw_round_trip(port, [request[:9], request[9:23], request[23:], pipeline])
    expected_prefix = b"+OK\r\n$5\r\nvalue\r\n:1\r\n-"
    if not reply.startswith(expected_prefix):
        print(f"  RESP framing/pipeline mismatch: {reply!r}")
        mismatches += 1

    candidate.flushdb()
    if candidate.dbsize() != 0:
        print("  FLUSHDB mismatch: database is not empty")
        mismatches += 1
    return mismatches


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
                print(
                    f"  MISMATCH op[{i}] {op.label} {op.args[0]}: oracle={expected} candidate={actual}"
                )
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
        inflight = sum(
            pool.map(
                lambda tid: _stress_client(
                    args.port, oracle_port, tid, per_thread, args.num_keys, args.seed
                ),
                range(args.threads),
            )
        )

    candidate_conns = [_client(args.port) for _ in range(args.verify_conns)]
    stress_keys = sorted(oracle.keys("*"))
    stress_recon = _reconcile(oracle, candidate_conns, stress_keys, "concurrent")

    hash_keys = [f"shared:h:{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        fanin_errors = sum(
            pool.map(
                lambda tid: _fanin_client(args.port, oracle_port, tid, hash_keys),
                range(args.threads),
            )
        )
    fanin = fanin_errors + _reconcile(oracle, candidate_conns, hash_keys, "shared-hash")

    print(
        f"  concurrent: threads={args.threads} ops={per_thread * args.threads} "
        f"inflight_mismatches={inflight} reconciled_keys={len(stress_keys)} "
        f"reconcile_mismatches={stress_recon}"
    )
    hot = _shared_hot_phase(args.port, args.threads)

    print(f"  shared-hash: keys={len(hash_keys)} mismatches={fanin}")
    print(f"  shared-hot: mismatches={hot}")
    return inflight + stress_recon + fanin + hot


def _shared_hot_phase(port, threads):
    """Exercise shared records under racing reads, writes, and deletes."""
    conn = _client(port)
    conn.flushdb()
    hash_keys = [f"hot:h:{i}" for i in range(4)]
    fields = [f"f{i}" for i in range(4)]
    for key in hash_keys:
        conn.hset(key, mapping={field: "v0" for field in fields})

    allowed = {f"v{i}" for i in range(threads + 1)}

    def writer(tid):
        client = _client(port)
        for key in hash_keys:
            for field in fields:
                client.hset(key, field, f"v{tid + 1}")
        return 0

    def reader(_):
        client = _client(port)
        errors = 0
        for _ in range(32):
            for key in hash_keys:
                snapshot = client.hgetall(key)
                if set(snapshot) != set(fields) or any(
                    value not in allowed for value in snapshot.values()
                ):
                    errors += 1
        return errors

    workers = max(2, threads)
    with ThreadPoolExecutor(max_workers=workers * 2) as pool:
        writes = [pool.submit(writer, tid) for tid in range(workers)]
        reads = [pool.submit(reader, tid) for tid in range(workers)]
        errors = sum(future.result() for future in writes + reads)

    final_values = {field: f"final:{field}" for field in fields}
    for key in hash_keys:
        conn.hset(key, mapping=final_values)
    for candidate_conn in [_client(port) for _ in range(8)]:
        for key in hash_keys:
            if candidate_conn.hgetall(key) != final_values:
                errors += 1

    delete_keys = [f"hot:delete:{i}" for i in range(64)]
    for key in delete_keys:
        conn.set(key, "value")

    def deleter(tid):
        client = _client(port)
        return sum(client.delete(key) for key in delete_keys[tid::workers])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        deleted = sum(pool.map(deleter, range(workers)))
    if deleted != len(delete_keys) or any(conn.get(key) is not None for key in delete_keys):
        errors += 1
    return errors


def main():
    parser = argparse.ArgumentParser(description="KV store accuracy checker")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port of an already-running candidate. Omit to launch ./run.sh automatically.",
    )
    parser.add_argument("--num-ops", type=int, default=5000, help="Sequential-phase op count.")
    parser.add_argument("--num-keys", type=int, default=200, help="Keyspace size per namespace.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--threads",
        type=int,
        default=16,
        help="Concurrent client threads for the concurrency phase (matches the benchmark headline).",
    )
    parser.add_argument(
        "--concurrent-ops", type=int, default=20000, help="Total ops across all concurrent threads."
    )
    parser.add_argument(
        "--verify-conns",
        type=int,
        default=8,
        help="Fresh candidate connections used to fan reconciliation reads across worker processes.",
    )
    parser.add_argument(
        "--no-concurrent", action="store_true", help="Run only the sequential phase."
    )
    args = parser.parse_args()

    with candidate_server(workspace=_WORKSPACE, port=args.port) as managed:
        args.port = managed.port if managed is not None else args.port
        assert args.port is not None
        assert _wait_until_listening(args.port, timeout=5), (
            f"Candidate not responding on port {args.port}"
        )

        oracle, oracle_port = _start_oracle()
        candidate = _client(args.port)

        print("=== SEMANTICS AND RESP2 ===")
        mismatches = _semantic_phase(oracle, candidate, args.port)
        print(f"  semantic mismatches={mismatches}")

        oracle.flushdb()
        candidate.flushdb()
        print("=== SEQUENTIAL ===")
        mismatches += _sequential_phase(oracle, candidate, args.num_ops, args.num_keys, args.seed)
        print(f"  sequential: ops={args.num_ops} total_mismatches={mismatches}")

        if not args.no_concurrent:
            print("=== CONCURRENT ===")
            mismatches += _concurrent_phase(oracle, oracle_port, args)

        print(f"\nTotal mismatches: {mismatches}")
        print("ALL CHECKS PASSED" if mismatches == 0 else "ACCURACY CHECK FAILED")
        sys.exit(0 if mismatches == 0 else 1)


if __name__ == "__main__":
    main()
