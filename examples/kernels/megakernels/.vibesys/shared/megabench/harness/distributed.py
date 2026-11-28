"""Evaluate a submission in one process per GPU with per-rank auditing."""

from __future__ import annotations

import json
import secrets
import statistics
import tempfile
import time
import traceback
from datetime import timedelta
from pathlib import Path

import torch
import torch.distributed as dist
import torch.multiprocessing as mp

from ..cases import Case
from ..tasks.parallel import initialize
from ..workloads import make_inputs, oracle, reference
from ..tasks.workloads import regrade, set_trial_scenario
from .benchmark import _audit_launches, _measure
from .correctness import _compare, input_fingerprints, mutated_inputs


def agree(local_error: str | None) -> None:
    """Report rank-local errors before advancing to another collective stage."""
    errors = [None] * dist.get_world_size()
    dist.all_gather_object(errors, local_error)
    if any(errors):
        raise AssertionError("; ".join(f"rank {rank}: {error}" for rank, error in enumerate(errors) if error))


def shared_seed() -> int:
    values = [secrets.randbits(32) if dist.get_rank() == 0 else None]
    dist.broadcast_object_list(values, src=0)
    return values[0]


def fresh_inputs(case: Case, seed: int, device: str) -> dict:
    values, local_error = None, None
    try:
        values = make_inputs(case, seed, device)
    except Exception as exc:
        local_error = f"input generation failed: {type(exc).__name__}: {exc}"
    agree(local_error)
    return values


def _rank_worker(rank: int, contract: dict, submission: str, device_kind: str,
                 first_device: int, trials: int, warmup: int, reps: int,
                 rendezvous: str, output_dir: str) -> None:
    from .runner import _load_submission

    case = Case(**contract)
    device = f"cuda:{first_device + rank}" if device_kind == "cuda" else "cpu"
    if device_kind == "cuda":
        torch.cuda.set_device(first_device + rank)
    result = {"rank": rank, "device": device}
    initialized = False
    try:
        dist.init_process_group("nccl" if device_kind == "cuda" else "gloo",
                                init_method=rendezvous, world_size=case.gpus, rank=rank,
                                timeout=timedelta(minutes=10),
                                device_id=torch.device(device) if device_kind == 'cuda' else None)
        initialized = True
        ctx = initialize(case)
        execution = {"rank": rank, "world_size": case.gpus, "tp_rank": ctx.tp_rank,
                     "ep_rank": ctx.ep_rank, "backend": dist.get_backend(), "device": device,
                     "tp_group": ctx.tp_group, "ep_group": ctx.ep_group}
        local_error, candidate = None, None
        start = time.perf_counter()
        try:
            candidate = _load_submission(Path(submission), case, execution=execution)
        except Exception as exc:
            local_error = f"build failed: {type(exc).__name__}: {exc}"
        agree(local_error)
        result["build_ms"] = (time.perf_counter() - start) * 1000
        result["correctness"] = {"status": "pass", "trials": []}
        with torch.inference_mode():
            for trial in range(trials):
                seed = shared_seed()
                inputs = fresh_inputs(case, seed, device)
                scenario = set_trial_scenario(case, inputs, trial)
                originals = input_fingerprints(inputs)
                expected = reference(case, inputs)
                exact = oracle(case, inputs)
                local_error, details = None, None
                try:
                    first_start = time.perf_counter() if trial == 0 else None
                    actual = candidate(inputs)
                    if device_kind == "cuda":
                        torch.cuda.synchronize(device)
                    if first_start is not None:
                        result["first_call_ms_including_jit"] = (time.perf_counter()-first_start)*1000
                    expected, exact = regrade(case, inputs, expected, exact, actual)
                    details = _compare(expected, actual, case, device, exact)
                    for name in mutated_inputs(inputs, originals):
                        raise AssertionError(f"candidate mutated input {name}")
                except Exception as exc:
                    local_error = f"trial {trial}: {type(exc).__name__}: {exc}"
                agree(local_error)
                result["correctness"]["trials"].append({"seed": seed, "scenario": scenario, "outputs": details})
                del inputs, originals, expected, exact, actual
            perf_inputs = fresh_inputs(case, shared_seed(), device)
            dist.barrier()
            result["candidate_timing"] = _measure(candidate, perf_inputs, device, warmup, reps)
            dist.barrier()
            result["reference_timing"] = _measure(lambda values: reference(case, values), perf_inputs, device, warmup, reps)
            dist.barrier()
            result["launch_audit"] = _audit_launches(candidate, perf_inputs, device, case.max_gpu_launches)
            dist.barrier()
            result["reference_launch_audit"] = _audit_launches(lambda values: reference(case, values), perf_inputs,
                                                              device, case.max_gpu_launches)
        if device_kind == "cuda":
            result["hardware"] = {"gpu_name": torch.cuda.get_device_name(device),
                                  "peak_allocated_bytes": torch.cuda.max_memory_allocated(device)}
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="incorrect" if isinstance(exc, AssertionError) else "error",
                      reason=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc(limit=12))
    finally:
        with (Path(output_dir) / f"rank-{rank}.json").open("x") as file:
            json.dump(result, file)
        if initialized:
            dist.destroy_process_group()


def worst_rank_timing(ranks: list[dict], key: str) -> dict:
    """Use the slowest rank on each repetition, including its device completion."""
    result = {}
    for prefix in ("host", "cuda_event"):
        series = [rank[key][prefix + "_ms"] for rank in ranks]
        values = [max(row) for row in zip(*series, strict=True)]
        ordered = sorted(values)
        result[prefix + "_ms"] = values
        result[prefix + "_p50_ms"] = statistics.median(values) if values else None
        if values:
            at = (len(values) - 1) * 0.95
            lo = int(at)
            result[prefix + "_p95_ms"] = ordered[lo] + (ordered[min(lo + 1, len(values) - 1)] - ordered[lo]) * (at - lo)
        else:
            result[prefix + "_p95_ms"] = None
    return result


def evaluate_distributed(case: Case, submission: Path, *, device: str,
                         trials: int, warmup: int, reps: int) -> dict:
    selected = torch.device(device)
    first_device = selected.index or 0
    if selected.type == "cuda" and torch.cuda.device_count() < first_device + case.gpus:
        return {"status": "unavailable", "reason": f"requires {case.gpus} visible GPUs starting at {device}"}
    with tempfile.TemporaryDirectory(prefix="megabench-ranks-") as directory:
        rendezvous = Path(directory) / "rendezvous"
        mp.spawn(_rank_worker, nprocs=case.gpus, join=True, args=(case.to_dict(), str(submission),
                 selected.type, first_device, trials, warmup, reps, rendezvous.as_uri(), directory))
        ranks = [json.loads((Path(directory) / f"rank-{rank}.json").read_text()) for rank in range(case.gpus)]
    failures = [rank for rank in ranks if rank["status"] != "pass"]
    if failures:
        return {"status": "incorrect" if any(rank["status"] == "incorrect" for rank in failures) else "error",
                "reason": failures[0]["reason"], "ranks": ranks}
    audits = [rank["launch_audit"] for rank in ranks]
    within_budget = all(audit["status"] == "within_budget" for audit in audits)
    failed_budget = any(audit["status"] == "launch_budget_failed" for audit in audits)
    status = ("correctness_only" if selected.type == "cpu" else "ok_provisional" if within_budget else
              "non_megakernel" if failed_budget else "authenticity_unverified")
    result = {"status": status, "ranks": ranks, "build_ms": max(rank["build_ms"] for rank in ranks),
              "correctness": {"status": "pass", "per_rank": [rank["correctness"] for rank in ranks]},
              "first_call_ms_including_jit": max(rank["first_call_ms_including_jit"] for rank in ranks),
              "candidate_timing": worst_rank_timing(ranks, "candidate_timing"),
              "reference_timing": worst_rank_timing(ranks, "reference_timing"),
              "launch_audit": {"status": "within_budget" if within_budget else
                               "launch_budget_failed" if failed_budget else "unverified",
                               "max_gpu_launches_per_rank": case.max_gpu_launches,
                               "per_rank": audits, "review_required": True},
              "topology": {"world_size": case.gpus, "tp": case.tp, "ep": case.ep,
                           "rank_mapping": "rank = ep_rank * tp + tp_rank"},
              "graph_baseline_unavailable": "The distributed eager reference uses host-driven routing and process-group collectives."}
    result["speedup_vs_eager_host"] = result["reference_timing"]["host_p50_ms"] / result["candidate_timing"]["host_p50_ms"]
    if selected.type == "cuda":
        ratio = result["reference_timing"]["cuda_event_p50_ms"] / result["candidate_timing"]["cuda_event_p50_ms"]
        result["speedup_vs_eager_cuda_event"] = ratio
        result["speedup_vs_best_baseline_cuda_event"] = ratio
    return result
