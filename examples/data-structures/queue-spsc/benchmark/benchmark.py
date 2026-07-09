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
from queue_input_core.contract import get_contract
from queue_input_core.reference import QueueFactory

SCENARIO = "spsc"


def main() -> None:
    contract = get_contract(SCENARIO)
    parser = argparse.ArgumentParser(
        description=f"Throughput benchmark for {contract.description}."
    )
    add_benchmark_arguments(parser, contract, load_config())
    parser.add_argument("--use-reference", action="store_true")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    config = benchmark_config_from_args(args)
    queue_cls = QueueFactory if args.use_reference else load_candidate(required=False)
    queue = make_queue(queue_cls or QueueFactory, contract, config.capacity)
    result = run_benchmark(queue, contract, config)
    print_benchmark_result(result)
    if args.output_json:
        write_benchmark_results([result], args.output_json)


if __name__ == "__main__":
    main()
