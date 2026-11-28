"""VibeSys adapter for exactly one assigned MegaBench case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from dataclasses import replace

from ..cases import load_regimes, select_cases
from ..harness.correctness import check_trials
from ..harness.runner import _load_submission, evaluate_case
from . import gpu_noise


def pinned_score(result: dict, pin_dir: Path | None, key: dict) -> tuple[float, dict | None]:
    """The progress score against one best-baseline time pinned per run.

    The baselines are re-measured at each evaluation and drift between rounds,
    so a faster kernel could score below a slower one measured earlier. The
    first successful evaluation in *pin_dir* writes its measured best baseline
    (the fastest of eager, graph and compiled); every later evaluation with the
    same *key* scores against that value. Without *pin_dir* the score uses the
    measured baseline, as before.
    """
    if result.get("status") != "ok_provisional":
        return 0.0, None
    measured = result["speedup_vs_best_baseline_cuda_event"]
    if pin_dir is None:
        return 1.0 + measured / (1.0 + measured), None
    cand = result["candidate_timing"]["cuda_event_p50_ms"]
    measured_best = measured * cand
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]
    path = Path(pin_dir) / f"score-baseline-{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    pinned_now = False
    if not path.is_file():
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": key, "best_baseline_cuda_event_p50_ms": measured_best},
                                  sort_keys=True, default=str))
        try:
            os.link(tmp, path)  # fails if another evaluation pinned first
            pinned_now = True
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    pinned = json.loads(path.read_text())["best_baseline_cuda_event_p50_ms"]
    speedup = pinned / cand
    info = {"pinned_best_baseline_cuda_event_p50_ms": pinned,
            "measured_best_baseline_cuda_event_p50_ms": measured_best,
            "speedup_vs_pinned_baseline_cuda_event": speedup,
            "pinned_now": pinned_now, "file": str(path)}
    return 1.0 + speedup / (1.0 + speedup), info


def stabilize(result: dict, case, case_id: str, submission: Path,
              regimes_path: Path | None) -> None:
    """Time the candidate in more fresh processes; a slow evaluate process is not scored.

    When the evaluate process is outside the fast cluster (``gpu_noise.spread``), its
    candidate p50 is replaced by the fast-cluster p50. With MEGABENCH_OFFICIAL_TIMING_HISTORY
    set, the p50 is also capped by the best earlier official run of the same code tree. The
    speedups are scaled to match. The evaluate value and all process values stay in
    ``timing_stability``.
    """
    processes = int(os.environ.get("MEGABENCH_STABILITY_PROCESSES", "3"))
    timing = result.get("candidate_timing") or {}
    if processes < 2 or case.gpus != 1 or timing.get("cuda_event_p50_ms") is None:
        return
    probes = gpu_noise.run_probes(
        gpu_noise.probe_command(case_id, submission, regimes_path), processes - 1)
    stab = gpu_noise.check_report(result, probes, gpu_noise.DEFAULT_TOLERANCE_PCT)
    result["timing_stability"] = stab
    old = timing["cuda_event_p50_ms"]
    new = stab["fast_p50_ms"] if 0 in stab["slow_processes"] else old
    # The framework's own history (a path the agent cannot write): a run of a tree that is
    # slower than its best earlier official run was in the slow mode as a whole.
    raw = os.environ.get(gpu_noise.OFFICIAL_HISTORY_ENV)
    history = Path(raw) if raw else None
    if history is not None:
        key = gpu_noise.history_key("official", case_id,
                                    regimes_path.name if regimes_path else "",
                                    gpu_noise.tree_digest(submission.parent))
        gpu_noise.score(stab, gpu_noise.best_known(history, key))
        gpu_noise.add_history(history, key, stab, "official")
        new = min(new, stab["score_ms"])
    if new != old:
        timing["cuda_event_p50_ms_evaluate_process"] = old
        timing["cuda_event_p50_ms"] = new
        for key in [k for k in result if k.startswith("speedup_vs_") and "cuda_event" in k]:
            result[key] *= old / new


# The metric VibeSys optimizes: the candidate's own CUDA-event p50 step time, lower is better.
# It names no field inside ``result``, because VibeSys searches nested JSON for the metric
# when the top level lacks it, and a failed candidate must have no reading at all.
METRIC = "kernel_latency_ms"
# The relative standard deviation of one benchmark run (objectives.toml [pareto]
# relative_noise). VibeSys uses it twice: a checkpoint is retained only when it beats the best
# by more than this fraction, and the framework verdict on an idea treats it as the floor of
# one run's sigma and asks for a gain beyond 2 * sigma * sqrt(1/m + 1/n) over m candidate and n
# base runs (2.8% at one run a side, 2.0% at two). It is a sigma, not the widest gap: kernel
# time varies by under 0.25% (CV) on one GPU, and its per-GPU means vary by 0.16% (CSM),
# 0.28% (pi0.5) and 0.96% (Waypoint) across the 8 B200s of one node (2026-11-07), which matters
# because a candidate and its base can land on different GPUs.
RELATIVE_NOISE = 0.01
# How the benchmark mode measures; vibesys_noise_calibration repeats exactly this.
BENCHMARK_SETTINGS = {"trials": 3, "warmup": 1, "reps": 3, "graph_baseline": False}


def kernel_latency_ms(result: dict) -> float | None:
    """The step time VibeSys compares, or None when the candidate has no valid reading.

    Only an ``ok_provisional`` result is a trusted megakernel measurement; anything else
    (incorrect, over the launch budget, unverified, an error) reports no latency, so it can
    never win on speed. The eager baseline is deliberately not involved: it varies by several
    percent between runs and would add its noise to every comparison.
    """
    if result.get("status") != "ok_provisional":
        return None
    latency = result.get("candidate_timing", {}).get("cuda_event_p50_ms")
    return float(latency) if latency is not None else None


def progress_score(result: dict) -> float:
    """The legacy saturating score, kept for reports; VibeSys no longer optimizes it."""
    speedup = result.get("speedup_vs_best_baseline_cuda_event")
    if result.get("status") != "ok_provisional" or speedup is None:
        return 0.0
    return 1.0 + speedup / (1.0 + speedup)


def benchmark_output(case_id: str, result: dict) -> dict:
    """The result file VibeSys reads back, without raw per-rank records.

    Multi-GPU results keep every rank's raw record, including the eager
    reference's launch audit, under ``ranks``. Their summaries are already in
    ``correctness`` and ``launch_audit``, so the result file omits the copies.
    """
    compact = {key: value for key, value in result.items() if key != "ranks"}
    if "regimes" in compact:  # the per-sequence contexts are already in result["case"]
        compact["regimes"] = [{key: value for key, value in row.items() if key != "contexts"}
                              for row in compact["regimes"]]
    output = {"case": case_id, "status": result.get("status")}
    latency = kernel_latency_ms(result)
    if latency is not None:
        output[METRIC] = latency
    return output | {"progress_score": progress_score(result), "result": compact}


def main(case_id: str, argv: list[str] | None = None,
         regimes_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("accuracy", "benchmark"))
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    case = select_cases("all", [case_id])[0]
    if regimes_path is not None:
        case = replace(case, regimes=load_regimes(regimes_path))
    submission = args.submission.resolve()
    if not submission.is_file():
        parser.error(f"missing submission: {submission}")
    if args.mode == "accuracy":
        import torch

        try:
            if case.gpus > 1:
                result = evaluate_case(case, submission, device="cuda:0", trials=3,
                                       warmup=0, reps=1, graph_baseline=False)
                if result.get("correctness", {}).get("status") != "pass":
                    raise AssertionError(result.get("reason", result["status"]))
            else:
                # Each regime shape is built and checked on its own.
                for shape in ([sub for _, sub in case.regime_cases()]
                              if case.regimes else [case]):
                    candidate = _load_submission(submission, shape)
                    with torch.inference_mode():
                        check_trials(shape, candidate, "cuda:0", 3)
        except Exception as exc:
            print(json.dumps({"case": case_id, "status": "fail",
                              "reason": f"{type(exc).__name__}: {exc}"}))
            return 1
        print(json.dumps({"case": case_id, "status": "pass"}))
        return 0
    if args.output_json is None:
        parser.error("benchmark requires --output-json")
    try:
        result = evaluate_case(case, submission, device="cuda:0", **BENCHMARK_SETTINGS)
    except Exception as exc:
        result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    stabilize(result, case, case_id, submission, regimes_path)
    # VibeSys optimizes METRIC (the stabilized candidate time). progress_score is for reports;
    # with MEGABENCH_BASELINE_CACHE it is scored against the run's pinned best baseline.
    pin_dir = os.environ.get("MEGABENCH_BASELINE_CACHE")
    key = {"case": case.to_dict()}
    if pin_dir and result.get("status") == "ok_provisional":
        import torch

        key["gpu"] = torch.cuda.get_device_name(0)
    score, pin = pinned_score(result, Path(pin_dir) if pin_dir else None, key)
    if pin is not None:
        result["score_baseline"] = pin
    output = benchmark_output(case_id, result)
    if pin is not None:
        output["progress_score"] = score
    with args.output_json.open("x", encoding="utf-8") as file:
        json.dump(output, file)
    summary = {key: output[key] for key in ("case", "status", METRIC, "progress_score")
               if key in output}
    if METRIC not in output:  # VibeSys rejects the run for the missing metric; say why
        summary["reason"] = result.get("reason") or (
            f"status {result.get('status')!r} is not ok_provisional, so the candidate has "
            f"no {METRIC} reading")
    print(json.dumps(summary))
    return 0
