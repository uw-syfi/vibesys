"""Check and benchmark one megakernel case with the vendored MegaBench harness beside this file.

    python evaluate.py accuracy --task TASK
    python evaluate.py benchmark --task TASK --output-json PATH

Task ``<workload>-<hw>`` grades the candidate ``<hw>/<workload>/submission.py`` under the
project root on the MegaBench case in ``.vibesys/tasks/<task>/case.json``.

The check passes when the outputs match the PyTorch reference on fresh inputs and the step stays
within its launch budget. The benchmark reports the step's device-event p50 as
``kernel_latency_ms``, and only for a candidate that passes both.
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # .vibesys/shared -> the project root
# The harness re-imports megabench in fresh processes (timing probes), so they need it too.
os.environ["PYTHONPATH"] = str(HERE)
sys.path.insert(0, str(HERE))

from megabench.cases import select_cases  # noqa: E402
from megabench.harness.runner import evaluate_case  # noqa: E402
from megabench.integrations.vibesys_case_evaluator import (  # noqa: E402
    BENCHMARK_SETTINGS,
    METRIC,
    kernel_latency_ms,
    stabilize,
)

DEVICE = "cuda:0"  # NVIDIA and ROCm builds of PyTorch both address the GPU as cuda


def resolve(task: str) -> tuple[str, Path]:
    """The case id and the candidate submission of *task*."""
    case_file = PROJECT / ".vibesys" / "tasks" / task / "case.json"
    if "/" in task or not case_file.is_file():
        raise SystemExit(f"unknown task: {task}")
    workload, _, hw = task.rpartition("-")
    return json.loads(case_file.read_text())["id"], PROJECT / hw / workload / "submission.py"


def summary(case_id: str, result: dict) -> dict:
    out = {"case": case_id, "status": result.get("status"), "reason": result.get("reason")}
    for key in ("correctness", "launch_audit", "input_mutation"):
        if key in result:
            out[key] = result[key]
    timing = result.get("candidate_timing") or {}
    if "cuda_event_p50_ms" in timing:
        out["cuda_event_p50_ms"] = timing["cuda_event_p50_ms"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("accuracy", "benchmark"))
    parser.add_argument("--task", required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument(
        "--stability-processes",
        type=int,
        default=1,
        help="fresh processes that time the candidate again; 1 keeps the evaluate process's reading",
    )
    args = parser.parse_args()
    os.environ["MEGABENCH_STABILITY_PROCESSES"] = str(args.stability_processes)
    case_id, submission = resolve(args.task)
    case = select_cases("all", [case_id])[0]
    if not submission.is_file():
        parser.error(f"missing submission: {submission}")
    if args.mode == "accuracy":
        result = evaluate_case(
            case, submission, device=DEVICE, trials=3, warmup=1, reps=1, graph_baseline=False
        )
        print(json.dumps(summary(case_id, result), default=str))
        return 0 if result.get("status") == "ok_provisional" else 1
    if args.output_json is None:
        parser.error("benchmark needs --output-json")
    result = evaluate_case(case, submission, device=DEVICE, **BENCHMARK_SETTINGS)
    stabilize(result, case, case_id, submission, None)
    output = summary(case_id, result)
    latency = kernel_latency_ms(result)
    if latency is not None:
        output[METRIC] = latency
    with args.output_json.open("x", encoding="utf-8") as file:
        json.dump(output, file, default=str)
    print(json.dumps(output, default=str))
    return 0 if latency is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
