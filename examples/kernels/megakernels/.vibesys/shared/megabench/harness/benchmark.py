"""Measure candidate and reference timings and audit GPU launches."""

from __future__ import annotations

import statistics
import time
from collections import Counter
from typing import Callable

from ..heartbeat import beat


def _measure(fn: Callable, inputs: dict, device: str, warmup: int,
             reps: int) -> dict:
    import torch

    cuda = torch.device(device).type == "cuda"
    for _ in range(warmup):
        fn(inputs)
        beat("warmup call")
    if cuda:
        torch.cuda.synchronize(device)
    beat("warmup done")
    host_ms: list[float] = []
    event_ms: list[float] = []
    for _ in range(reps):
        if cuda:
            torch.cuda.synchronize(device)
            start_event = torch.cuda.Event(enable_timing=True)
            end_event = torch.cuda.Event(enable_timing=True)
            start_event.record()
        start = time.perf_counter()
        fn(inputs)
        if cuda:
            end_event.record()
            end_event.synchronize()
            event_ms.append(start_event.elapsed_time(end_event))
        host_ms.append((time.perf_counter() - start) * 1000)
        beat("timed call")

    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        at = (len(ordered) - 1) * 0.95
        lo = int(at)
        return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] -
                              ordered[lo]) * (at - lo)

    return {"host_ms": host_ms, "host_p50_ms": statistics.median(host_ms),
            "host_p95_ms": p95(host_ms),
            "cuda_event_ms": event_ms,
            "cuda_event_p50_ms": statistics.median(event_ms) if cuda else None,
            "cuda_event_p95_ms": p95(event_ms) if cuda else None}


# An eager multi-GPU reference launches tens of thousands of kernels per step.
# Every name is kept only for small counts, such as a candidate's launches.
_MAX_LISTED_KERNEL_NAMES = 32
_TOP_KERNEL_NAMES = 16


def _kernel_name_summary(names: list[str]) -> dict:
    """List small sets of launched kernel names; summarize large ones by frequency."""
    if len(names) <= _MAX_LISTED_KERNEL_NAMES:
        return {"gpu_kernel_names": names}
    counts = Counter(names)
    return {"gpu_kernel_names": names[:_MAX_LISTED_KERNEL_NAMES],
            "gpu_kernel_names_truncated": True,
            "distinct_gpu_kernel_names": len(counts),
            "top_gpu_kernel_names": [{"name": name, "count": count}
                                     for name, count in counts.most_common(_TOP_KERNEL_NAMES)]}


def _audit_launches(fn: Callable, inputs: dict, device: str,
                    launch_budget: int) -> dict:
    import torch

    if torch.device(device).type != "cuda":
        return {"status": "not_applicable_cpu"}
    beat("launch audit", allow=300, force=True)  # profiler start-up
    try:
        from torch.profiler import ProfilerActivity, profile

        with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
            fn(inputs)
            torch.cuda.synchronize(device)
        gpu_events = [event for event in prof.events()
                      if event.device_type == torch.autograd.DeviceType.CUDA]
        cpu_names = [event.name for event in prof.events()
                     if event.device_type == torch.autograd.DeviceType.CPU]
        count = len(gpu_events)
        graph_hint = any("cudagraphlaunch" in name.lower() for name in cpu_names)
        return {"status": "within_budget" if 1 <= count <= launch_budget else
                "launch_budget_failed", "gpu_kernel_count": count,
                "max_gpu_launches": launch_budget,
                **_kernel_name_summary([event.name for event in gpu_events]),
                "cuda_graph_runtime_hint": graph_hint,
                "review_required": True}
    except Exception as exc:
        return {"status": "unverified", "reason": f"{type(exc).__name__}: {exc}",
                "review_required": True}
