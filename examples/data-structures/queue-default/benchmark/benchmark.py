from __future__ import annotations

import argparse

from queue_input_core.benchmark import (
    add_benchmark_arguments,
    benchmark_config_from_args,
    make_queue,
    print_benchmark_result,
    run_benchmark,
    write_benchmark_results,
)
from queue_input_core.candidate import load_candidate
from queue_input_core.config import load_config
from queue_input_core.contract import SCENARIOS, get_contract
from queue_input_core.reference import QueueFactory


def main() -> None:
    input_config = load_config()
    parser = argparse.ArgumentParser(
        description="Throughput benchmark for VibeServe queue scenarios."
    )
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, "all"],
        default=input_config.scenario or "spsc",
    )
    add_benchmark_arguments(parser, None, input_config)
    parser.add_argument("--use-reference", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    config = benchmark_config_from_args(args)
    queue_cls = QueueFactory if args.use_reference else load_candidate(required=False)
    contracts = (
        [get_contract(scenario) for scenario in SCENARIOS]
        if args.scenario == "all"
        else [get_contract(args.scenario)]
    )

    results = []
    for contract in contracts:
        queue = make_queue(queue_cls or QueueFactory, contract, config.capacity)
        result = run_benchmark(queue, contract, config)
        print_benchmark_result(result)
        results.append(result)
    if args.output_json:
        write_benchmark_results(results, args.output_json)


if __name__ == "__main__":
    main()
