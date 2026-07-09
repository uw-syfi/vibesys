from __future__ import annotations

import argparse
import sys

from queue_input_core.candidate import load_candidate
from queue_input_core.checker import (
    add_check_arguments,
    check_config_from_args,
    print_check_results,
    run_checks,
)
from queue_input_core.config import load_config
from queue_input_core.contract import SCENARIOS, get_contract
from queue_input_core.reference import QueueFactory


def main() -> None:
    input_config = load_config()
    parser = argparse.ArgumentParser(
        description="Correctness checker for VibeServe queue scenarios."
    )
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, "all"],
        default=input_config.scenario or "all",
    )
    add_check_arguments(parser, None, input_config)
    parser.add_argument("--use-reference", action="store_true")
    args = parser.parse_args()

    print(
        "Loading reference QueueFactory ..."
        if args.use_reference
        else "Loading VibeServeQueue from main.py ..."
    )
    cls = QueueFactory if args.use_reference else load_candidate()
    print("  Loaded.")

    contracts = (
        [get_contract(scenario) for scenario in SCENARIOS]
        if args.scenario == "all"
        else [get_contract(args.scenario)]
    )
    results = run_checks(cls, contracts, check_config_from_args(args))
    print_check_results(results)
    sys.exit(0 if all(result.ok for result in results.values()) else 1)


if __name__ == "__main__":
    main()
