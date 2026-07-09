from __future__ import annotations

import argparse
import sys

from queue_input_core.candidate import load_candidate
from queue_input_core.checker import (
    add_check_arguments,
    check_config_from_args,
    print_check_result,
    run_check,
)
from queue_input_core.config import load_config
from queue_input_core.contract import get_contract
from queue_input_core.reference import QueueFactory

SCENARIO = "mpsc"


def main() -> None:
    contract = get_contract(SCENARIO)
    parser = argparse.ArgumentParser(description=f"Correctness checker for {contract.description}.")
    add_check_arguments(parser, contract, load_config())
    parser.add_argument("--use-reference", action="store_true")
    args = parser.parse_args()

    print(
        "Loading reference QueueFactory ..."
        if args.use_reference
        else "Loading VibeServeQueue from main.py ..."
    )
    cls = QueueFactory if args.use_reference else load_candidate()
    print("  Loaded.")

    print(f"[{contract.name.upper()}] Checking ...")
    result = run_check(cls, contract, check_config_from_args(args))
    print_check_result(result)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
