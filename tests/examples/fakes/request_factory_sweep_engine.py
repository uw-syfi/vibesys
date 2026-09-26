#!/usr/bin/env python3
"""Configurable Request Factory CLI fake for the Llama sweep contract tests."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(name)
    return value


def _take(pool: list[int], offset: int, length: int) -> list[int]:
    return [pool[(offset + index) % len(pool)] for index in range(length)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--token-pool-limit", type=int, required=True)
    parser.add_argument("--max-concurrency", type=int, required=True)
    parser.add_argument("--summary-path", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    args, _unknown = parser.parse_known_args()

    with args.trace.open(newline="", encoding="utf-8") as source:
        requests = list(csv.DictReader(source))
    pool = [int(token) + 2 for token in args.text_file.read_text(encoding="utf-8").split()]
    pool = pool[: args.token_pool_limit]
    prompts = [
        _take(pool, ordinal * 9_973, int(request["input_len"]))
        for ordinal, request in enumerate(requests)
    ]
    capture_path = Path(_required_environment("RF_FAKE_CAPTURE"))
    with capture_path.open("a", encoding="utf-8") as capture:
        capture.write(json.dumps({"concurrency": args.max_concurrency, "prompts": prompts}) + "\n")

    state_path = Path(_required_environment("RF_FAKE_STATE"))
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    key = str(args.max_concurrency)
    observation_index = state.get(key, 0)
    observations = json.loads(_required_environment("RF_FAKE_POINTS"))[key]
    throughput, latency_ms = observations[observation_index]
    state[key] = observation_index + 1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    count = len(requests)
    args.summary_path.write_text(
        json.dumps(
            {
                "replay": {
                    "common": {
                        "attempted_steps": count,
                        "success_steps": count,
                        "failed_steps": 0,
                        "output_mismatch_steps": 0,
                        "output_token_throughput_per_s": throughput,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with args.log_path.open("w", encoding="utf-8") as request_log:
        for _request in requests:
            request_log.write(
                json.dumps({"outcome": {"error": None, "total_duration_ms": latency_ms}}) + "\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
