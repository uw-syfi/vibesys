from __future__ import annotations

import argparse
import json
import random
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from queue_input_core.config import QueueInputConfig
from queue_input_core.contract import QueueContract
from queue_input_core.trace import (
    TraceCollection,
    collect_trace,
    make_linearizability_workload,
    to_porcupine_history,
)


@dataclass(frozen=True)
class CheckConfig:
    capacity: int
    ops: int
    producers: int
    consumers: int
    seed: int
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str
    trace: TraceCollection | None = None


def default_check_config(
    contract: QueueContract | None,
    input_config: QueueInputConfig | None = None,
) -> CheckConfig:
    return CheckConfig(
        capacity=input_config.capacity
        if input_config and input_config.capacity is not None
        else (contract.default_capacity if contract else 64),
        ops=2000,
        producers=input_config.producers
        if input_config and input_config.producers is not None
        else (contract.default_producers if contract else 4),
        consumers=input_config.consumers
        if input_config and input_config.consumers is not None
        else (contract.default_consumers if contract else 4),
        seed=42,
    )


def add_check_arguments(
    parser: argparse.ArgumentParser,
    contract: QueueContract | None,
    input_config: QueueInputConfig | None = None,
) -> None:
    defaults = default_check_config(contract, input_config)
    parser.add_argument("--capacity", type=int, default=defaults.capacity)
    parser.add_argument("--ops", type=int, default=defaults.ops)
    parser.add_argument("--producers", type=int, default=defaults.producers)
    parser.add_argument("--consumers", type=int, default=defaults.consumers)
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--timeout-seconds", type=float, default=defaults.timeout_seconds)


def check_config_from_args(args: argparse.Namespace) -> CheckConfig:
    return CheckConfig(
        capacity=args.capacity,
        ops=args.ops,
        producers=args.producers,
        consumers=args.consumers,
        seed=args.seed,
        timeout_seconds=args.timeout_seconds,
    )


def run_porcupine_checker(history: list[dict], capacity: int) -> None:
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


def check_linearizable_queue(cls, contract: QueueContract, config: CheckConfig) -> CheckResult:
    queue = cls(scenario=contract.name, capacity=config.capacity)
    plan = make_linearizability_workload(
        contract,
        ops=config.ops,
        producers=config.producers,
        consumers=config.consumers,
        seed=config.seed,
    )
    trace = collect_trace(queue, plan, timeout_seconds=config.timeout_seconds)

    if trace.timed_out:
        return false_result(
            f"{contract.name.upper()} timed out while collecting history",
            trace=trace,
        )
    if trace.errors:
        return false_result(
            f"{contract.name.upper()} trace collection failed: {'; '.join(trace.errors)}",
            trace=trace,
        )

    try:
        run_porcupine_checker(to_porcupine_history(trace.events), config.capacity)
    except Exception as exc:
        return false_result(f"{contract.name.upper()} linearizability failure: {exc}", trace=trace)

    return CheckResult(
        True,
        (
            f"{contract.name.upper()} linearizable "
            f"({len(trace.events)} ops, {trace.producers}P/{trace.consumers}C, "
            f"capacity={config.capacity})"
        ),
        trace=trace,
    )


def false_result(message: str, trace: TraceCollection | None = None) -> CheckResult:
    return CheckResult(False, message, trace=trace)


def check_lossy(cls, contract: QueueContract, config: CheckConfig) -> CheckResult:
    queue = cls(scenario=contract.name, capacity=config.capacity)
    rng = random.Random(config.seed)
    enqueued, dequeued = set(), []
    for i in range(config.ops):
        if rng.random() < 0.6:
            ok = queue.enqueue(i)
            if not ok:
                return false_result(f"Lossy enqueue returned False at op {i}")
            enqueued.add(i)
        else:
            x = queue.dequeue()
            if x is not None:
                dequeued.append(x)
        if queue.size() > config.capacity:
            return false_result(f"size {queue.size()} > capacity {config.capacity}")
    fabricated = set(dequeued) - enqueued
    if fabricated:
        return false_result(f"Lossy returned items never enqueued: {list(fabricated)[:5]}")
    return CheckResult(
        True,
        f"Lossy OK ({config.ops} ops, capacity={config.capacity}, dequeued={len(dequeued)})",
    )


def check_batch(cls, contract: QueueContract, config: CheckConfig) -> CheckResult:
    queue = cls(scenario=contract.name, capacity=config.capacity)
    rng = random.Random(config.seed)
    enqueued, dequeued = [], []
    for i in range(config.ops):
        if rng.random() < 0.6:
            if queue.enqueue(i):
                enqueued.append(i)
        else:
            batch = queue.dequeue()
            if not isinstance(batch, list):
                return false_result(f"Batch dequeue must return list, got {type(batch).__name__}")
            dequeued.extend(batch)
    fabricated = set(dequeued) - set(enqueued)
    if fabricated:
        return false_result(f"Batch returned items never enqueued: {list(fabricated)[:5]}")
    duplicates = len(dequeued) - len(set(dequeued))
    if duplicates:
        return false_result(f"Batch returned {duplicates} duplicates")
    return CheckResult(
        True,
        f"Batch OK ({config.ops} ops, capacity={config.capacity}, dequeued={len(dequeued)})",
    )


def run_check(cls, contract: QueueContract, config: CheckConfig) -> CheckResult:
    if contract.linearizable_fifo:
        return check_linearizable_queue(cls, contract, config)
    if contract.lossy:
        return check_lossy(cls, contract, config)
    if contract.batched_dequeue:
        return check_batch(cls, contract, config)
    return false_result(f"Unknown scenario: {contract.name}")


def run_checks(
    cls,
    contracts: Iterable[QueueContract],
    config: CheckConfig,
) -> dict[str, CheckResult]:
    return {contract.name: run_check(cls, contract, config) for contract in contracts}


def print_check_result(result: CheckResult) -> None:
    print(f"  PASS - {result.message}" if result.ok else f"  FAIL - {result.message}")


def print_check_results(results: Mapping[str, CheckResult]) -> None:
    for scenario, result in results.items():
        print(f"[{scenario.upper()}] Checking ...")
        print_check_result(result)
    passed = sum(result.ok for result in results.values())
    print(f"Results: {passed}/{len(results)} passed")
