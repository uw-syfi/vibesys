"""CLI correctness gate; the evaluator owns workload, HTTP, and process effects.

Public interface: ``main(argv=None)`` and the documented command line. Successful
exit means both fresh randomized documents passed; errors retain raw artifacts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4


def main(argv: list[str] | None = None) -> int:
    """Check changed facts and permuted question order on one owned fresh server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir", type=Path, default=Path("artifacts") / f"correctness-{uuid4().hex}"
    )
    parser.add_argument("--seed", type=int, help="Replay a saved workload seed.")
    parser.add_argument("--model-path", type=Path, help="Existing cached model snapshot.")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evaluator import EvaluationError, run_evaluation

    try:
        result = run_evaluation(
            "correctness", args.artifact_dir, args.seed, model_path=args.model_path
        )
    except (EvaluationError, OSError, ValueError) as exc:
        print(f"Correctness failed: {exc}", file=sys.stderr)
        return 1
    print(f"Correctness passed: {len(result.observations)} scored responses.")
    print(f"Artifacts: {args.artifact_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
