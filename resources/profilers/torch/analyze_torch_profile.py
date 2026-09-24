#!/usr/bin/env python3
"""Torch profiler analysis toolkit — subcommand-based.

This is the in-process counterpart to analyze_nsys.py. Unlike nsys,
``torch.profiler`` uses CUPTI's Callback API and does **not** need access
to ``/proc/driver/nvidia/`` to sync GPU clocks, so it works in execution
environments that do not expose host driver instrumentation.

Usage:
    # Capture a profile by loading VibeServeModel from main.py and
    # running .generate(...) under torch.profiler:
    python analyze_torch_profile.py capture \\
        --model-dir /workspace --output prof.json [--num-iters 50] \\
        [--prompt "The capital of France is"] [--max-tokens 32]

    # Or capture against a running HTTP server with an injected profile
    # endpoint (see README for the expected /admin/... contract):
    python analyze_torch_profile.py capture-server \\
        --url http://localhost:8000 --output prof.json [--requests 10]

    # Analyze the saved profile:
    python analyze_torch_profile.py kernels prof.json [--top 15]
    python analyze_torch_profile.py operators prof.json [--top 15]
    python analyze_torch_profile.py memory prof.json
    python analyze_torch_profile.py summary prof.json    # all-in-one (runs certify first)

    # Certify and analyze a raw Kineto/Chrome trace (e.g. vLLM's
    # VLLM_TORCH_PROFILER_DIR output, a *.pt.trace.json(.gz) file). kernels,
    # operators, memory, cpu-overhead, tables, and summary above also accept
    # this file directly -- they auto-detect and convert it in-process.
    python analyze_torch_profile.py certify trace.pt.trace.json.gz
    python analyze_torch_profile.py gemm-shapes trace.pt.trace.json.gz \\
        [--top 20] [--out shapes.json]
    python analyze_torch_profile.py roofline trace.pt.trace.json.gz \\
        [--device mi210 | --peak-tflops 181 --peak-gbps 1600]

Output schema (``prof.json``):
    {
        "version": 1,
        "captured_at": "ISO-8601",
        "mode": "model" | "server",
        "device": "cuda",
        "dtype": "bfloat16",
        "num_iters": int,
        "total_cuda_time_us": float,
        "total_cpu_time_us": float,
        "events": [
            {
                "name": str,
                "category": "kernel" | "operator" | "memory" | "cpu",
                "cpu_time_us": float,
                "cuda_time_us": float,
                "count": int,
                "self_cuda_time_us": float,
                "self_cpu_time_us": float,
            },
            ...
        ]
    }
"""  # noqa: D301, EXE001  # tracked: #288

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Capture: in-process (loads VibeServeModel from main.py)
# ---------------------------------------------------------------------------


def _load_main_module(model_dir: str):  # noqa: ANN202  # tracked: #288
    """Import ``main.py`` from *model_dir* and return the module.

    The agent's server always exports ``VibeServeModel`` from ``main.py``,
    matching the accuracy-checker contract.
    """
    main_path = Path(model_dir) / "main.py"
    if not main_path.is_file():
        raise FileNotFoundError(  # noqa: TRY003  # tracked: #288
            f"main.py not found at {main_path} — pass --model-dir pointing "
            f"to the workspace root that contains main.py."
        )
    spec = importlib.util.spec_from_file_location("vs_main", str(main_path))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(main_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        if str(main_path.parent) in sys.path:
            sys.path.remove(str(main_path.parent))
    if not hasattr(module, "VibeServeModel"):
        raise AttributeError(  # noqa: TRY003  # tracked: #288
            f"main.py at {main_path} does not export VibeServeModel. "
            f"The accuracy-checker interface requires this symbol."
        )
    return module


def cmd_capture(args: argparse.Namespace) -> None:
    """Profile VibeServeModel.generate under torch.profiler, dump JSON."""
    import torch  # noqa: PLC0415  # tracked: #288
    from torch.profiler import ProfilerActivity, profile  # noqa: PLC0415  # tracked: #288

    dtype = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }.get(args.dtype, torch.bfloat16)

    model_dir = args.model_dir
    weights_dir = args.weights_dir or "/model"

    print(  # noqa: T201  # tracked: #288
        f"[capture] loading VibeServeModel from {model_dir}/main.py "
        f"(weights: {weights_dir}, device={args.device}, dtype={args.dtype})",
        file=sys.stderr,
    )

    module = _load_main_module(model_dir)
    model = module.VibeServeModel.from_pretrained(
        weights_dir,
        device=args.device,
        dtype=dtype,
    )

    # Tokenizer: if VibeServeModel doesn't expose one, fall back to
    # transformers directly against weights_dir.
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        from transformers import AutoTokenizer  # noqa: PLC0415  # tracked: #288

        tokenizer = AutoTokenizer.from_pretrained(weights_dir)

    input_ids = tokenizer(args.prompt, return_tensors="pt").input_ids.to(args.device)

    # Warmup — first call compiles kernels, allocates KV cache, etc.
    print(f"[capture] warmup ({args.warmup} iters)...", file=sys.stderr)  # noqa: T201  # tracked: #288
    for _ in range(args.warmup):
        with torch.no_grad():
            model.generate(input_ids=input_ids, max_new_tokens=args.max_tokens)
    torch.cuda.synchronize()

    print(  # noqa: T201  # tracked: #288
        f"[capture] profiling ({args.num_iters} iters, max_new_tokens={args.max_tokens})...",
        file=sys.stderr,
    )
    t0 = time.time()
    with profile(
        activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        record_shapes=False,
        profile_memory=True,
        with_stack=False,
    ) as prof:
        with torch.no_grad():
            for _ in range(args.num_iters):
                model.generate(input_ids=input_ids, max_new_tokens=args.max_tokens)
        torch.cuda.synchronize()
    wall = time.time() - t0
    print(f"[capture] elapsed {wall:.2f}s", file=sys.stderr)  # noqa: T201  # tracked: #288

    events_json = _summarize_prof(prof, num_iters=args.num_iters)
    events_json.update(
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "mode": "model",
            "device": args.device,
            "dtype": args.dtype,
            "num_iters": args.num_iters,
            "max_new_tokens": args.max_tokens,
            "prompt": args.prompt,
            "wall_time_sec": wall,
        }
    )
    Path(args.output).write_text(json.dumps(events_json, indent=2))
    print(  # noqa: T201  # tracked: #288
        f"[capture] wrote {args.output} "
        f"({events_json['total_cuda_time_us']:.0f} us CUDA, "
        f"{events_json['total_cpu_time_us']:.0f} us CPU, "
        f"{len(events_json['events'])} events)",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Capture: against a running server (needs /admin/profile endpoints)
# ---------------------------------------------------------------------------


def cmd_capture_server(args: argparse.Namespace) -> None:
    """Capture a profile from a running server.

    Expects the server to expose two admin endpoints:
      POST /admin/profile/start -> {"ok": true}
      POST /admin/profile/stop  -> {"events": [...], "total_cuda_time_us": ..., ...}

    The agent must add these endpoints to main.py if they want
    server-path profiling (captures HTTP/batching overhead).  When
    absent, use ``capture`` (in-process) instead.
    """
    import urllib.request  # noqa: PLC0415  # tracked: #288

    def _post(path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310  # tracked: #288
            args.url.rstrip("/") + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:  # noqa: S310  # tracked: #288
            return json.loads(resp.read())

    print(f"[capture-server] POST {args.url}/admin/profile/start", file=sys.stderr)  # noqa: T201  # tracked: #288
    _post("/admin/profile/start")

    print(  # noqa: T201  # tracked: #288
        f"[capture-server] sending {args.requests} requests (max_tokens={args.max_tokens})...",
        file=sys.stderr,
    )
    for _i in range(args.requests):
        _post(
            "/v1/completions",
            {
                "prompt": args.prompt,
                "max_tokens": args.max_tokens,
                "temperature": 0,
            },
        )

    print(f"[capture-server] POST {args.url}/admin/profile/stop", file=sys.stderr)  # noqa: T201  # tracked: #288
    result = _post("/admin/profile/stop")

    if "events" not in result:
        raise RuntimeError(  # noqa: TRY003  # tracked: #288
            "/admin/profile/stop response missing 'events' key — "
            "is the server implementing the expected contract?"
        )

    result.setdefault("captured_at", datetime.now(UTC).isoformat())
    result.setdefault("mode", "server")
    result.setdefault("num_iters", args.requests)
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"[capture-server] wrote {args.output}", file=sys.stderr)  # noqa: T201  # tracked: #288


# ---------------------------------------------------------------------------
# Helpers: summarize torch.profiler events into JSON
# ---------------------------------------------------------------------------


def _summarize_prof(prof, num_iters: int) -> dict:  # noqa: ANN001, ARG001  # tracked: #288
    """Extract a structured summary from a torch.profiler profile object."""
    totals = prof.key_averages()
    events: list[dict] = []
    total_cuda = 0.0
    total_cpu = 0.0
    for ev in totals:
        # Use torch.profiler's device_time / cpu_time (microseconds, per-event
        # sums — cpu_time is the inclusive CPU time; device_time is the
        # GPU time). self_* are exclusive (for leaf analysis).
        cuda_us = float(getattr(ev, "device_time_total", 0.0) or 0.0)
        cpu_us = float(getattr(ev, "cpu_time_total", 0.0) or 0.0)
        self_cuda_us = float(getattr(ev, "self_device_time_total", 0.0) or 0.0)
        self_cpu_us = float(getattr(ev, "self_cpu_time_total", 0.0) or 0.0)
        name = ev.key
        # Classify
        if (
            ev.device_type == torch.autograd.DeviceType.CUDA
            or "cuda" in name.lower()
            or (cuda_us > 0 and cpu_us < cuda_us / 4)
        ):
            category = "kernel"
        elif name.startswith("aten::") or name.startswith("torch::"):  # noqa: PIE810  # tracked: #288
            category = "operator"
        elif (
            "memcpy" in name.lower()
            or "memset" in name.lower()
            or "malloc" in name.lower()
            or "free" in name.lower()
        ):
            category = "memory"
        else:
            category = "cpu"
        events.append(
            {
                "name": name,
                "category": category,
                "cpu_time_us": cpu_us,
                "cuda_time_us": cuda_us,
                "self_cpu_time_us": self_cpu_us,
                "self_cuda_time_us": self_cuda_us,
                "count": int(ev.count),
            }
        )
        total_cuda += self_cuda_us
        total_cpu += self_cpu_us
    return {
        "version": 1,
        "total_cuda_time_us": total_cuda,
        "total_cpu_time_us": total_cpu,
        "num_events": len(events),
        "events": events,
    }


# Lazy import to keep module importable without torch
try:
    import torch
except ImportError:  # pragma: no cover
    torch = None  # type: ignore  # noqa: PGH003  # tracked: #288


# ---------------------------------------------------------------------------
# Analysis subcommands
# ---------------------------------------------------------------------------


def _read_json_maybe_gz(path: str) -> dict:
    """Read a JSON file, transparently decompressing a ``.gz`` suffix.

    vLLM's ``/stop_profile`` and ``torch.profiler``'s own Chrome-trace export
    both commonly gzip the trace (``*.pt.trace.json.gz``).
    """
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(Path(path).read_text())


def _is_chrome_trace(data: dict) -> bool:
    """True for a raw Kineto/Chrome trace (``traceEvents``), not our summarized report."""
    return isinstance(data, dict) and "traceEvents" in data


def _load(path: str) -> dict:
    """Load either our summarized ``prof.json`` or a raw Kineto/Chrome trace.

    A raw trace is converted in-process into the same summarized schema
    ``_summarize_prof`` produces, so ``kernels``/``operators``/``memory``/
    ``cpu-overhead``/``tables``/``summary`` all work directly on a real
    vLLM-captured ``*.pt.trace.json(.gz)`` file, not just our own ``capture``
    output.
    """
    raw = _read_json_maybe_gz(path)
    if _is_chrome_trace(raw):
        return _summarize_chrome_trace(raw)
    return raw


def _fmt_us(us: float) -> str:
    if us >= 1_000_000:  # noqa: PLR2004  # tracked: #288
        return f"{us / 1_000_000:.2f} s"
    if us >= 1000:  # noqa: PLR2004  # tracked: #288
        return f"{us / 1000:.2f} ms"
    return f"{us:.1f} us"


def cmd_kernels(args: argparse.Namespace) -> None:
    """Top GPU kernels by total self-CUDA time."""
    data = _load(args.report)
    kernels = [e for e in data["events"] if e["self_cuda_time_us"] > 0]
    kernels.sort(key=lambda e: e["self_cuda_time_us"], reverse=True)
    total = data["total_cuda_time_us"] or 1.0
    print(f"Total self-CUDA time: {_fmt_us(total)}")  # noqa: T201  # tracked: #288
    print()  # noqa: T201  # tracked: #288
    print(f"{'Name':<60}{'Self CUDA':>14}{'% of total':>12}{'Count':>10}")  # noqa: T201  # tracked: #288
    print("-" * 96)  # noqa: T201  # tracked: #288
    for ev in kernels[: args.top]:
        pct = 100.0 * ev["self_cuda_time_us"] / total
        name = ev["name"]
        if len(name) > 58:  # noqa: PLR2004  # tracked: #288
            name = name[:55] + "..."
        print(f"{name:<60}{_fmt_us(ev['self_cuda_time_us']):>14}{pct:>11.1f}%{ev['count']:>10}")  # noqa: T201  # tracked: #288


def cmd_operators(args: argparse.Namespace) -> None:
    """Top operators (aten::*, torch::*) by CPU time."""
    data = _load(args.report)
    ops = [e for e in data["events"] if e["category"] == "operator"]
    ops.sort(key=lambda e: e["self_cpu_time_us"], reverse=True)
    total_cpu = data["total_cpu_time_us"] or 1.0
    print(f"Total self-CPU time: {_fmt_us(total_cpu)}")  # noqa: T201  # tracked: #288
    print()  # noqa: T201  # tracked: #288
    print(f"{'Operator':<50}{'Self CPU':>14}{'CUDA time':>14}{'% CPU':>10}{'Count':>10}")  # noqa: T201  # tracked: #288
    print("-" * 98)  # noqa: T201  # tracked: #288
    for ev in ops[: args.top]:
        pct = 100.0 * ev["self_cpu_time_us"] / total_cpu
        name = ev["name"]
        if len(name) > 48:  # noqa: PLR2004  # tracked: #288
            name = name[:45] + "..."
        print(  # noqa: T201  # tracked: #288
            f"{name:<50}{_fmt_us(ev['self_cpu_time_us']):>14}"
            f"{_fmt_us(ev['cuda_time_us']):>14}{pct:>9.1f}%{ev['count']:>10}"
        )


def cmd_cpu_overhead(args: argparse.Namespace) -> None:
    """CPU vs GPU time breakdown — detects launch-bound scenarios."""
    data = _load(args.report)
    total_cpu = data["total_cpu_time_us"]
    total_cuda = data["total_cuda_time_us"]
    ratio = total_cpu / total_cuda if total_cuda else float("inf")
    print(f"Total self-CPU time:  {_fmt_us(total_cpu)}")  # noqa: T201  # tracked: #288
    print(f"Total self-CUDA time: {_fmt_us(total_cuda)}")  # noqa: T201  # tracked: #288
    print(f"CPU/CUDA ratio:       {ratio:.2f}x")  # noqa: T201  # tracked: #288
    if ratio > 2.0:  # noqa: PLR2004  # tracked: #288
        print()  # noqa: T201  # tracked: #288
        print(  # noqa: T201  # tracked: #288
            "Interpretation: CPU time dominates (>2x GPU). Likely launch-bound"
            " — consider CUDA graphs, fewer kernels, or larger batches."
        )
    elif ratio < 0.5:  # noqa: PLR2004  # tracked: #288
        print()  # noqa: T201  # tracked: #288
        print(  # noqa: T201  # tracked: #288
            "Interpretation: GPU time dominates (<0.5x CPU). Compute-bound —"
            " focus on kernel fusion, flash attention, better algorithms."
        )
    else:
        print()  # noqa: T201  # tracked: #288
        print(  # noqa: T201  # tracked: #288
            "Interpretation: CPU and GPU roughly balanced. Both axes may benefit from optimization."
        )


def cmd_memory(args: argparse.Namespace) -> None:
    """Memory allocation / transfer events."""
    data = _load(args.report)
    mem = [e for e in data["events"] if e["category"] == "memory"]
    mem.sort(key=lambda e: e["cuda_time_us"] + e["cpu_time_us"], reverse=True)
    if not mem:
        print("(no memory events recorded)")  # noqa: T201  # tracked: #288
        return
    print(f"{'Operation':<40}{'Total CUDA':>14}{'Total CPU':>14}{'Count':>10}")  # noqa: T201  # tracked: #288
    print("-" * 78)  # noqa: T201  # tracked: #288
    for ev in mem:
        name = ev["name"]
        if len(name) > 38:  # noqa: PLR2004  # tracked: #288
            name = name[:35] + "..."
        print(  # noqa: T201  # tracked: #288
            f"{name:<40}{_fmt_us(ev['cuda_time_us']):>14}"
            f"{_fmt_us(ev['cpu_time_us']):>14}{ev['count']:>10}"
        )


def cmd_summary(args: argparse.Namespace) -> None:
    """All-in-one: certify (raw traces only) + overhead + kernels + operators + memory."""
    args.top = getattr(args, "top", 15)
    print("=" * 80)  # noqa: T201  # tracked: #288
    print("  TORCH PROFILER SUMMARY")  # noqa: T201  # tracked: #288
    print("=" * 80)  # noqa: T201  # tracked: #288

    raw = _read_json_maybe_gz(args.report)
    if _is_chrome_trace(raw):
        print("\n## Trace Certification\n")  # noqa: T201  # tracked: #288
        index = _index_trace(raw)
        op_to_kernels = _build_op_to_kernels(index)
        _print_certify(_certify(index, op_to_kernels))
        data = _summarize_chrome_trace(raw)
    else:
        data = raw
    print(f"\nCaptured: {data.get('captured_at', '?')}")  # noqa: T201  # tracked: #288
    print(f"Mode:     {data.get('mode', '?')}")  # noqa: T201  # tracked: #288
    print(f"Device:   {data.get('device', '?')} ({data.get('dtype', '?')})")  # noqa: T201  # tracked: #288
    if "wall_time_sec" in data:
        print(f"Wall:     {data['wall_time_sec']:.2f}s over {data.get('num_iters', '?')} iters")  # noqa: T201  # tracked: #288
    print("\n## CPU / GPU Overhead\n")  # noqa: T201  # tracked: #288
    cmd_cpu_overhead(args)
    print("\n## Top GPU Kernels\n")  # noqa: T201  # tracked: #288
    cmd_kernels(args)
    print("\n## Top Operators\n")  # noqa: T201  # tracked: #288
    cmd_operators(args)
    print("\n## Memory Operations\n")  # noqa: T201  # tracked: #288
    cmd_memory(args)


def cmd_tables(args: argparse.Namespace) -> None:
    """List available analyses (mirrors nsys 'tables' convention)."""
    data = _load(args.report)
    categories: dict[str, int] = {}
    for ev in data["events"]:
        categories[ev["category"]] = categories.get(ev["category"], 0) + 1
    print(f"Captured:   {data.get('captured_at', '?')}")  # noqa: T201  # tracked: #288
    print(f"Mode:       {data.get('mode', '?')}")  # noqa: T201  # tracked: #288
    print(f"Num events: {data.get('num_events', len(data.get('events', [])))}")  # noqa: T201  # tracked: #288
    print(f"Total CUDA: {_fmt_us(data.get('total_cuda_time_us', 0))}")  # noqa: T201  # tracked: #288
    print(f"Total CPU:  {_fmt_us(data.get('total_cpu_time_us', 0))}")  # noqa: T201  # tracked: #288
    print("\nEvent categories:")  # noqa: T201  # tracked: #288
    for cat, n in sorted(categories.items(), key=lambda kv: -kv[1]):
        print(f"  {cat:<10} {n:>6} events")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        "\nAvailable subcommands: kernels, operators, cpu-overhead, memory, summary, "
        "certify, gemm-shapes, roofline"
    )


# ---------------------------------------------------------------------------
# Raw Kineto/Chrome trace indexing (certify, gemm-shapes, roofline)
#
# The commands above operate on our own summarized ``prof.json``.  These
# three need per-call detail a summary discards: per-op "Input Dims", and
# the correlation between a cpu_op and the GPU kernel(s) it launched.  That
# detail only survives in the raw Chrome trace JSON ``torch.profiler``
# exports (``prof.export_chrome_trace(...)``), which is also exactly what
# vLLM's ``POST /stop_profile`` writes under ``VLLM_TORCH_PROFILER_DIR``
# (``*.pt.trace.json(.gz)``).
# ---------------------------------------------------------------------------

_GEMM_OPS: dict[str, str] = {
    "aten::mm": "mm",
    "aten::addmm": "addmm",
    "aten::bmm": "bmm",
    "aten::baddbmm": "baddbmm",
    "aten::linear": "linear",
    "aten::matmul": "matmul",
    "aten::_scaled_mm": "scaled_mm",
}

# Index into an op's "Input Dims" / "Input type" args that carries the
# GEMM-defining left operand, used to pick a representative dtype. addmm and
# baddbmm take (bias, mat1, mat2); the rest take (mat1, mat2, ...).
_GEMM_DTYPE_INDEX: dict[str, int] = {
    "aten::mm": 0,
    "aten::matmul": 0,
    "aten::addmm": 1,
    "aten::baddbmm": 1,
    "aten::linear": 0,
    "aten::bmm": 0,
    "aten::_scaled_mm": 0,
}

_ATTN_OPS: dict[str, str] = {
    "aten::scaled_dot_product_attention": "sdpa",
    "aten::_scaled_dot_product_flash_attention": "sdpa_flash",
    "aten::_scaled_dot_product_efficient_attention": "sdpa_efficient",
}

_BYTES_PER_ELEM: dict[str, int] = {
    "float": 4,
    "float32": 4,
    "double": 8,
    "float64": 8,
    "c10::Half": 2,
    "half": 2,
    "float16": 2,
    "c10::BFloat16": 2,
    "bfloat16": 2,
    "c10::Float8_e4m3fn": 1,
    "c10::Float8_e5m2": 1,
    "c10::Float8_e4m3fnuz": 1,  # ROCm fp8 variant
    "c10::Float8_e5m2fnuz": 1,  # ROCm fp8 variant
    "int8": 1,
    "char": 1,
    "signed char": 1,
    "unsigned char": 1,
    "int": 4,
    "int32": 4,
    "long": 8,
    "long int": 8,
    "int64": 8,
}
_DEFAULT_ELEM_BYTES = 2  # unknown dtype: assume bf16, the LLM-serving default


def _elem_bytes(dtype: str) -> int:
    return _BYTES_PER_ELEM.get(dtype, _DEFAULT_ELEM_BYTES)


@dataclass(frozen=True)
class DevicePeak:
    """Nominal dense (no-sparsity) matrix-core peak for one GPU SKU."""

    label: str
    peak_tflops: float
    peak_gbps: float


# Vendor spec-sheet numbers (bf16/fp16 dense matrix-core peak, HBM
# bandwidth). Best-effort and approximate -- pass --peak-tflops/--peak-gbps
# for an authoritative figure.
_DEVICE_PEAKS: dict[str, DevicePeak] = {
    "mi210": DevicePeak("AMD Instinct MI210", 181.0, 1600.0),
    "mi300x": DevicePeak("AMD Instinct MI300X", 1307.4, 5300.0),
    "mi300a": DevicePeak("AMD Instinct MI300A", 980.6, 5300.0),
    "mi325x": DevicePeak("AMD Instinct MI325X", 1307.4, 6000.0),
    "mi355x": DevicePeak("AMD Instinct MI355X", 2512.0, 8000.0),
    "h100": DevicePeak("NVIDIA H100 SXM", 989.4, 3350.0),
}

_DEVICE_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("MI210", "mi210"),
    ("MI300X", "mi300x"),
    ("MI300A", "mi300a"),
    ("MI325X", "mi325x"),
    ("MI355X", "mi355x"),
    ("H100", "h100"),
)


def _detect_device_key(raw: dict) -> str | None:
    """Best-effort device match from the trace's ``deviceProperties`` name."""
    for prop in raw.get("deviceProperties") or []:
        name = str(prop.get("name", "")).upper()
        for hint, key in _DEVICE_NAME_HINTS:
            if hint in name:
                return key
    return None


@dataclass
class TraceIndex:
    """Categorized events pulled from one pass over ``traceEvents``."""

    cpu_ops: list[dict]
    kernels: list[dict]
    runtime_launches: list[dict]
    user_annotations: list[dict]
    memory_events: list[dict]
    device_properties: list[dict]
    trace_ts_min: float
    trace_ts_max: float


_KERNEL_CATS = {"kernel", "Kernel"}
_RUNTIME_CATS = {"cuda_runtime", "hip_runtime", "runtime", "Runtime"}
_MEMORY_CATS = {"gpu_memcpy", "gpu_memset", "Memcpy", "Memset"}
_ANNOTATION_CATS = {"user_annotation", "cpu_instant_event"}


def _index_trace(raw: dict) -> TraceIndex:
    """Single pass over ``traceEvents``, bucketed by Kineto category."""
    events = raw.get("traceEvents") or []
    cpu_ops: list[dict] = []
    kernels: list[dict] = []
    runtime_launches: list[dict] = []
    annotations: list[dict] = []
    memory_events: list[dict] = []
    ts_min = math.inf
    ts_max = -math.inf
    for ev in events:
        if ev.get("ph") != "X":
            continue
        ts = ev.get("ts")
        dur = ev.get("dur") or 0
        if isinstance(ts, int | float):
            ts_min = min(ts_min, ts)
            ts_max = max(ts_max, ts + dur)
        cat = ev.get("cat", "")
        if cat == "cpu_op":
            cpu_ops.append(ev)
        elif cat in _KERNEL_CATS:
            kernels.append(ev)
        elif cat in _RUNTIME_CATS:
            runtime_launches.append(ev)
        elif cat in _ANNOTATION_CATS:
            annotations.append(ev)
        elif cat in _MEMORY_CATS:
            memory_events.append(ev)
    if ts_min is math.inf:
        ts_min = ts_max = 0.0
    return TraceIndex(
        cpu_ops=cpu_ops,
        kernels=kernels,
        runtime_launches=runtime_launches,
        user_annotations=annotations,
        memory_events=memory_events,
        device_properties=list(raw.get("deviceProperties") or []),
        trace_ts_min=ts_min,
        trace_ts_max=ts_max,
    )


def _external_id(ev: dict) -> int | None:
    return (ev.get("args") or {}).get("External id")


def _correlation_id(ev: dict) -> int | None:
    return (ev.get("args") or {}).get("correlation")


def _index_by_correlation(kernels: list[dict]) -> dict[int, list[dict]]:
    corr_to_kernels: dict[int, list[dict]] = {}
    for k in kernels:
        corr = _correlation_id(k)
        if corr is not None:
            corr_to_kernels.setdefault(corr, []).append(k)
    return corr_to_kernels


def _index_by_external_id(events: list[dict]) -> dict[int, list[dict]]:
    ext_to_events: dict[int, list[dict]] = {}
    for ev in events:
        ext = _external_id(ev)
        if ext is not None:
            ext_to_events.setdefault(ext, []).append(ev)
    return ext_to_events


def _external_id_to_correlations(runtime_launches: list[dict]) -> dict[int, set[int]]:
    ext_to_corrs: dict[int, set[int]] = {}
    for launch in runtime_launches:
        ext = _external_id(launch)
        corr = _correlation_id(launch)
        if ext is not None and corr is not None:
            ext_to_corrs.setdefault(ext, set()).add(corr)
    return ext_to_corrs


def _build_op_to_kernels(index: TraceIndex) -> dict[int, list[dict]]:
    """Map each cpu_op's index in ``index.cpu_ops`` to the kernels it launched.

    Kineto links a cpu_op to the kernel(s) it launched through two hops in
    the common case: ``cpu_op.args["External id"] ==
    runtime_launch.args["External id"]``, and ``runtime_launch.args
    ["correlation"] == kernel.args["correlation"]``. Some exporters instead
    stamp "External id" directly on the kernel event; fall back to that
    when no runtime launch events are present (e.g. a trace captured
    without the runtime activity category).
    """
    corr_to_kernels = _index_by_correlation(index.kernels)
    ext_to_corrs = _external_id_to_correlations(index.runtime_launches)
    ext_to_kernels_direct = _index_by_external_id(index.kernels)

    result: dict[int, list[dict]] = {}
    for i, op in enumerate(index.cpu_ops):
        ext = _external_id(op)
        if ext is None:
            continue
        kernels: list[dict] = []
        for corr in ext_to_corrs.get(ext, ()):
            kernels.extend(corr_to_kernels.get(corr, ()))
        if not kernels:
            kernels = ext_to_kernels_direct.get(ext, [])
        if kernels:
            result[i] = kernels
    return result


def _merged_duration(events: list[dict]) -> float:
    """Union of ``[ts, ts+dur)`` intervals, so overlapping GPU streams don't double-count."""
    intervals = sorted(
        (e["ts"], e["ts"] + (e.get("dur") or 0))
        for e in events
        if isinstance(e.get("ts"), int | float)
    )
    merged = 0.0
    cur_start: float | None = None
    cur_end: float | None = None
    for start, end in intervals:
        if cur_end is None:
            cur_start, cur_end = start, end
        elif start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            merged += cur_end - cur_start
            cur_start, cur_end = start, end
    if cur_end is not None and cur_start is not None:
        merged += cur_end - cur_start
    return merged


def _self_time_by_name(cpu_ops: list[dict]) -> dict[str, dict]:
    """Aggregate self (exclusive) CPU time per op name via a per-thread call-stack sweep.

    Standard flame-graph self-time algorithm: within one (pid, tid) thread,
    ``cpu_op`` events nest as a proper call stack (RECORD_FUNCTION push/pop),
    so a child's duration is subtracted from its immediate parent's self
    time. Threads are independent and aggregated separately, then summed by
    op name.
    """
    by_thread: dict[tuple, list[int]] = {}
    for i, op in enumerate(cpu_ops):
        by_thread.setdefault((op.get("pid"), op.get("tid")), []).append(i)

    self_us = [float(op.get("dur") or 0) for op in cpu_ops]
    for idxs in by_thread.values():
        ordered = sorted(
            idxs, key=lambda i: (cpu_ops[i].get("ts") or 0, -(cpu_ops[i].get("dur") or 0))
        )
        stack: list[int] = []
        for i in ordered:
            ts = cpu_ops[i].get("ts") or 0
            dur = float(cpu_ops[i].get("dur") or 0)
            while stack:
                top = stack[-1]
                top_end = (cpu_ops[top].get("ts") or 0) + (cpu_ops[top].get("dur") or 0)
                if top_end <= ts:
                    stack.pop()
                else:
                    break
            if stack:
                self_us[stack[-1]] -= dur
            stack.append(i)

    agg: dict[str, dict] = {}
    for i, op in enumerate(cpu_ops):
        name = op.get("name", "unknown")
        entry = agg.setdefault(name, {"self_us": 0.0, "total_us": 0.0, "count": 0})
        entry["self_us"] += max(0.0, self_us[i])
        entry["total_us"] += float(op.get("dur") or 0)
        entry["count"] += 1
    return agg


def _summarize_chrome_trace(raw: dict) -> dict:
    """Convert a raw Kineto/Chrome trace into the ``_summarize_prof`` schema."""
    index = _index_trace(raw)
    events: list[dict] = []
    total_cuda = 0.0
    total_cpu = 0.0

    kernel_agg: dict[str, dict] = {}
    for k in index.kernels:
        entry = kernel_agg.setdefault(k.get("name", "unknown"), {"dur": 0.0, "count": 0})
        entry["dur"] += float(k.get("dur") or 0)
        entry["count"] += 1
    for name, entry in kernel_agg.items():
        events.append(
            {
                "name": name,
                "category": "kernel",
                "cpu_time_us": 0.0,
                "cuda_time_us": entry["dur"],
                "self_cpu_time_us": 0.0,
                "self_cuda_time_us": entry["dur"],
                "count": entry["count"],
            }
        )
        total_cuda += entry["dur"]

    for name, entry in _self_time_by_name(index.cpu_ops).items():
        category = "operator" if name.startswith(("aten::", "torch::")) else "cpu"
        events.append(
            {
                "name": name,
                "category": category,
                "cpu_time_us": entry["total_us"],
                "cuda_time_us": 0.0,
                "self_cpu_time_us": entry["self_us"],
                "self_cuda_time_us": 0.0,
                "count": entry["count"],
            }
        )
        total_cpu += entry["self_us"]

    mem_agg: dict[str, dict] = {}
    for m in index.memory_events:
        entry = mem_agg.setdefault(m.get("name", "memcpy"), {"dur": 0.0, "count": 0})
        entry["dur"] += float(m.get("dur") or 0)
        entry["count"] += 1
    for name, entry in mem_agg.items():
        events.append(
            {
                "name": name,
                "category": "memory",
                "cpu_time_us": 0.0,
                "cuda_time_us": entry["dur"],
                "self_cpu_time_us": 0.0,
                "self_cuda_time_us": entry["dur"],
                "count": entry["count"],
            }
        )

    return {
        "version": 1,
        "captured_at": None,
        "mode": "chrome_trace",
        "device": _detect_device_key(raw) or "unknown",
        "total_cuda_time_us": total_cuda,
        "total_cpu_time_us": total_cpu,
        "num_events": len(events),
        "events": events,
    }


# ---------------------------------------------------------------------------
# certify
# ---------------------------------------------------------------------------

_MIN_KERNELS_WARN = 5
_RECORD_SHAPES_FAIL = 0.0
_RECORD_SHAPES_WARN = 0.8
_GPU_BUSY_FAIL = 0.05
_GPU_BUSY_WARN = 0.3


@dataclass
class CertifyItem:
    """One PASS/WARN/FAIL line of a trace certification verdict."""

    status: str  # "PASS" | "WARN" | "FAIL"
    check: str
    detail: str
    fix: str | None = None


def _looks_like_step_marker(name: str) -> bool:
    return name.lower().startswith(("profilerstep", "execute_"))


def _certify(index: TraceIndex, op_to_kernels: dict[int, list[dict]]) -> list[CertifyItem]:  # noqa: C901, PLR0912  # tracked: #288
    items: list[CertifyItem] = []
    n_ops = len(index.cpu_ops)
    n_kernels = len(index.kernels)

    if n_kernels == 0:
        items.append(
            CertifyItem(
                "FAIL",
                "gpu_kernels",
                "0 GPU kernel events found.",
                "Re-capture with the CUDA activity enabled (torch.profiler."
                "ProfilerActivity.CUDA also covers HIP kernels on ROCm) and confirm "
                "GPU work actually ran during the capture window.",
            )
        )
    elif n_kernels < _MIN_KERNELS_WARN:
        items.append(
            CertifyItem(
                "WARN",
                "gpu_kernels",
                f"Only {n_kernels} GPU kernel events -- window may be too narrow.",
                "Widen the capture: more iterations, or a longer profiler schedule 'active' phase.",
            )
        )
    else:
        items.append(CertifyItem("PASS", "gpu_kernels", f"{n_kernels} GPU kernel events."))

    if n_ops == 0:
        items.append(
            CertifyItem(
                "WARN",
                "cpu_ops",
                "0 cpu_op events -- operator-level attribution (gemm-shapes, roofline) "
                "is unavailable.",
                "Enable torch.profiler.ProfilerActivity.CPU in the capture.",
            )
        )
    else:
        items.append(CertifyItem("PASS", "cpu_ops", f"{n_ops} cpu_op events."))

    if n_ops:
        with_dims = sum(1 for op in index.cpu_ops if (op.get("args") or {}).get("Input Dims"))
        frac = with_dims / n_ops
        if frac <= _RECORD_SHAPES_FAIL:
            items.append(
                CertifyItem(
                    "FAIL",
                    "record_shapes",
                    "0% of cpu_op events carry 'Input Dims'.",
                    "Re-capture with record_shapes=True passed to torch.profiler."
                    "profile(...) -- required for gemm-shapes and roofline.",
                )
            )
        elif frac < _RECORD_SHAPES_WARN:
            items.append(
                CertifyItem(
                    "WARN",
                    "record_shapes",
                    f"Only {frac:.0%} of cpu_op events carry 'Input Dims'.",
                    "Some ops were captured without shapes; re-capture with "
                    "record_shapes=True for full coverage.",
                )
            )
        else:
            items.append(
                CertifyItem(
                    "PASS", "record_shapes", f"{frac:.0%} of cpu_op events carry 'Input Dims'."
                )
            )

    step_markers = [a for a in index.user_annotations if _looks_like_step_marker(a.get("name", ""))]
    if step_markers:
        items.append(
            CertifyItem(
                "PASS", "step_markers", f"{len(step_markers)} step/annotation markers found."
            )
        )
    else:
        items.append(
            CertifyItem(
                "WARN",
                "step_markers",
                "No ProfilerStep#/execute_* annotation markers found.",
                "Wrap the profiled loop in torch.profiler's schedule(wait=..., "
                "warmup=..., active=...) so ProfilerStep# markers appear, or add "
                "record_function('execute_<phase>') around each iteration.",
            )
        )

    window_us = index.trace_ts_max - index.trace_ts_min
    if window_us > 0 and n_kernels > 0:
        busy_frac = _merged_duration(index.kernels) / window_us
        if busy_frac < _GPU_BUSY_FAIL:
            items.append(
                CertifyItem(
                    "FAIL",
                    "gpu_busy",
                    f"GPU busy only {busy_frac:.1%} of the {window_us / 1000:.1f} ms "
                    "capture window -- host appears idle.",
                    "Re-capture under sustained load (e.g. drive concurrent requests "
                    "against the server) instead of a single idle iteration.",
                )
            )
        elif busy_frac < _GPU_BUSY_WARN:
            items.append(
                CertifyItem(
                    "WARN",
                    "gpu_busy",
                    f"GPU busy {busy_frac:.1%} of the capture window.",
                    "Consider capturing under higher concurrency for a more representative window.",
                )
            )
        else:
            items.append(
                CertifyItem("PASS", "gpu_busy", f"GPU busy {busy_frac:.1%} of the capture window.")
            )

    graph_launches = [
        ev
        for ev in (*index.runtime_launches, *index.kernels)
        if "graphlaunch" in ev.get("name", "").lower()
    ]
    if graph_launches:
        items.append(
            CertifyItem(
                "WARN",
                "graph_replay",
                f"{len(graph_launches)} hipGraphLaunch/cudaGraphLaunch events found.",
                "Per-op kernel attribution is degraded under graph replay: kernels "
                "launched by a captured graph do not carry the originating cpu_op's "
                "correlation. Trust only aggregate kernel-level numbers, or re-capture "
                "in eager mode (graphs disabled) for op-level attribution.",
            )
        )
    else:
        items.append(
            CertifyItem(
                "PASS",
                "graph_replay",
                "No graph replay launches detected -- kernel-to-op attribution should be reliable.",
            )
        )

    if n_ops and n_kernels and not op_to_kernels:
        items.append(
            CertifyItem(
                "WARN",
                "correlation",
                "No cpu_op -> kernel correlation could be established.",
                "gemm-shapes/roofline need the 'External id'/'correlation' args "
                "Kineto stamps on cpu_op, runtime, and kernel events; re-capture "
                "without any post-processing that strips event 'args'.",
            )
        )

    return items


def _certify_verdict(items: list[CertifyItem]) -> str:
    statuses = {item.status for item in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _print_certify(items: list[CertifyItem]) -> None:
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    ordered = sorted(items, key=lambda i: order[i.status])
    print(f"Trace certification: {_certify_verdict(items)}")  # noqa: T201  # tracked: #288
    print()  # noqa: T201  # tracked: #288
    for item in ordered:
        print(f"[{item.status}] {item.check}: {item.detail}")  # noqa: T201  # tracked: #288
        if item.fix and item.status != "PASS":
            print(f"       -> {item.fix}")  # noqa: T201  # tracked: #288


def cmd_certify(args: argparse.Namespace) -> None:
    """Structural validity check on a raw Kineto/Chrome trace before trusting it."""
    raw = _read_json_maybe_gz(args.trace)
    if not _is_chrome_trace(raw):
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"{args.trace} is not a raw Kineto/Chrome trace (no 'traceEvents' key). "
            "certify expects the *.pt.trace.json(.gz) file torch.profiler / vLLM's "
            "/stop_profile writes, not a summarized prof.json."
        )
    index = _index_trace(raw)
    op_to_kernels = _build_op_to_kernels(index)
    _print_certify(_certify(index, op_to_kernels))


# ---------------------------------------------------------------------------
# gemm-shapes
# ---------------------------------------------------------------------------


@dataclass
class GemmShape:
    """One deduplicated (op, M, N, K, batch, dtype) GEMM demand bucket."""

    op: str
    m: int
    n: int
    k: int
    batch: int
    dtype: str
    call_count: int
    total_gpu_time_us: float


_RANK_2D = 2
_RANK_3D = 3


def _shape_dense_2d(a: list, b: list) -> tuple[int, int, int, int] | None:
    """(M,K) x (K,N) -> (M, N, K, batch=1), for mm/matmul/_scaled_mm/addmm."""
    if len(a) != _RANK_2D or len(b) != _RANK_2D:
        return None
    m, k = a
    k2, n = b
    return (m, n, k, 1) if k == k2 else None


def _shape_linear(a: list, w: list) -> tuple[int, int, int, int] | None:
    """input(..., K) x weight(N, K) -> (M, N, K, batch=1), flattening leading dims into M."""
    if len(a) < _RANK_2D or len(w) != _RANK_2D:
        return None
    m = 1
    for d in a[:-1]:
        m *= d
    k = a[-1]
    out_features, in_features = w
    return (m, out_features, k, 1) if k == in_features else None


def _shape_batched(a: list, b: list) -> tuple[int, int, int, int] | None:
    """(B,M,K) x (B,K,N) -> (M, N, K, B), for bmm/baddbmm."""
    if len(a) != _RANK_3D or len(b) != _RANK_3D:
        return None
    batch, m, k = a
    _batch2, k2, n = b
    return (m, n, k, batch) if k == k2 else None


# (min Input Dims length, index of operand A, index of operand B, shape fn)
# for each GEMM-family op. addmm/baddbmm carry a leading bias operand.
_GEMM_OPERAND_SPEC: dict[
    str, tuple[int, int, int, Callable[[list, list], tuple[int, int, int, int] | None]]
] = {
    "aten::mm": (_RANK_2D, 0, 1, _shape_dense_2d),
    "aten::matmul": (_RANK_2D, 0, 1, _shape_dense_2d),
    "aten::_scaled_mm": (_RANK_2D, 0, 1, _shape_dense_2d),
    "aten::addmm": (_RANK_3D, 1, 2, _shape_dense_2d),
    "aten::linear": (_RANK_2D, 0, 1, _shape_linear),
    "aten::bmm": (_RANK_2D, 0, 1, _shape_batched),
    "aten::baddbmm": (_RANK_3D, 1, 2, _shape_batched),
}


def _shape_for_gemm_op(op_name: str, dims: list) -> tuple[int, int, int, int] | None:
    """Return (M, N, K, batch) for a GEMM-family op's "Input Dims", or None if unrecognized."""
    spec = _GEMM_OPERAND_SPEC.get(op_name)
    if spec is None:
        return None
    min_len, idx_a, idx_b, handler = spec
    if len(dims) < min_len:
        return None
    try:
        return handler(dims[idx_a], dims[idx_b])
    except (TypeError, ValueError):
        return None


def _dtype_for_gemm_op(op_name: str, types: list | None) -> str:
    if not types:
        return "unknown"
    idx = _GEMM_DTYPE_INDEX.get(op_name, 0)
    return types[idx] if idx < len(types) else (types[0] if types else "unknown")


def _extract_gemm_shapes(
    index: TraceIndex, op_to_kernels: dict[int, list[dict]]
) -> list[GemmShape]:
    demand: dict[tuple, dict] = {}
    for i, op in enumerate(index.cpu_ops):
        name = op.get("name", "")
        if name not in _GEMM_OPS:
            continue
        args = op.get("args") or {}
        dims = args.get("Input Dims")
        if not dims:
            continue
        shape = _shape_for_gemm_op(name, dims)
        if shape is None:
            continue
        m, n, k, batch = shape
        dtype = _dtype_for_gemm_op(name, args.get("Input type"))
        key = (_GEMM_OPS[name], m, n, k, batch, dtype)
        gpu_time = sum(float(kv.get("dur") or 0) for kv in op_to_kernels.get(i, ()))
        entry = demand.setdefault(key, {"call_count": 0, "gpu_time_us": 0.0})
        entry["call_count"] += 1
        entry["gpu_time_us"] += gpu_time

    return [
        GemmShape(
            op=key[0],
            m=key[1],
            n=key[2],
            k=key[3],
            batch=key[4],
            dtype=key[5],
            call_count=v["call_count"],
            total_gpu_time_us=v["gpu_time_us"],
        )
        for key, v in demand.items()
    ]


def _shape_label(m: int, n: int, k: int, batch: int) -> str:
    return f"{batch}x{m}x{n}x{k}" if batch > 1 else f"{m}x{n}x{k}"


def _print_gemm_table(shapes: list[GemmShape]) -> None:
    if not shapes:
        print(  # noqa: T201  # tracked: #288
            "(no GEMM ops with both 'Input Dims' and a correlated GPU kernel found -- "
            "run `certify` on this trace first)"
        )
        return
    total = sum(s.total_gpu_time_us for s in shapes) or 1.0
    header = f"{'Op':<10}{'Shape (batch x M x N x K)':<28}{'Dtype':<18}{'Calls':>8}{'GPU time':>14}{'% total':>10}"
    print(header)  # noqa: T201  # tracked: #288
    print("-" * 88)  # noqa: T201  # tracked: #288
    for s in shapes:
        pct = 100.0 * s.total_gpu_time_us / total
        shape_str = _shape_label(s.m, s.n, s.k, s.batch)
        print(  # noqa: T201  # tracked: #288
            f"{s.op:<10}{shape_str:<28}{s.dtype:<18}{s.call_count:>8}"
            f"{_fmt_us(s.total_gpu_time_us):>14}{pct:>9.1f}%"
        )


def cmd_gemm_shapes(args: argparse.Namespace) -> None:
    """Extract (M, N, K, dtype) GEMM demand from a raw trace, ranked by GPU time."""
    raw = _read_json_maybe_gz(args.trace)
    if not _is_chrome_trace(raw):
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"{args.trace} is not a raw Kineto/Chrome trace (no 'traceEvents' key). "
            "gemm-shapes needs the *.pt.trace.json(.gz) file, not a summarized prof.json."
        )
    index = _index_trace(raw)
    op_to_kernels = _build_op_to_kernels(index)
    shapes = _extract_gemm_shapes(index, op_to_kernels)
    shapes.sort(key=lambda s: (s.total_gpu_time_us, s.call_count), reverse=True)
    top = shapes[: args.top] if args.top else shapes
    _print_gemm_table(top)
    if args.out:
        Path(args.out).write_text(json.dumps([asdict(s) for s in top], indent=2))
        print(f"\n[gemm-shapes] wrote {args.out} ({len(top)} shapes)", file=sys.stderr)  # noqa: T201  # tracked: #288


# ---------------------------------------------------------------------------
# roofline
# ---------------------------------------------------------------------------


@dataclass
class RooflineRow:
    """One op's achieved FLOP/s, GB/s, arithmetic intensity, and roofline bound class."""

    op: str
    shape: str
    dtype: str
    gpu_time_us: float
    flops: float
    total_bytes: float
    achieved_tflops: float
    achieved_gbps: float
    arithmetic_intensity: float
    pct_of_peak: float
    bound: str  # "compute" | "memory"


@dataclass
class _OpFlopsBytes:
    """FLOPs/bytes demand for one correlated op, before roofline math is applied."""

    op_label: str
    shape: str
    dtype: str
    gpu_time_us: float
    flops: float
    total_bytes: float


def _gemm_flops_bytes(m: int, n: int, k: int, batch: int, dtype: str) -> tuple[float, float]:
    elem = _elem_bytes(dtype)
    flops = 2.0 * batch * m * n * k
    total_bytes = float(batch) * (m * k + k * n + m * n) * elem
    return flops, total_bytes


_ATTN_SHAPE_RANK = 4
_ATTN_MIN_TENSORS = 3  # Q, K, V


def _attention_flops_bytes(dims: list, dtype: str) -> tuple[float, float] | None:
    """FLOPs/bytes for scaled-dot-product attention, if Q/K/V are (B, H, S, D)."""
    if len(dims) < _ATTN_MIN_TENSORS:
        return None
    q, k, v = dims[0], dims[1], dims[2]
    if not (
        len(q) == _ATTN_SHAPE_RANK and len(k) == _ATTN_SHAPE_RANK and len(v) == _ATTN_SHAPE_RANK
    ):
        return None
    b, h, s_q, d = q
    _b2, _h2, s_k, _d2 = k
    elem = _elem_bytes(dtype)
    flops = 4.0 * b * h * s_q * s_k * d  # QK^T + AV, each 2*B*H*Sq*Sk*D
    # Bytes read/written for Q, K, V, O -- ignores the (Sq x Sk) attention
    # matrix, which flash-attention-style kernels never materialize to HBM.
    total_bytes = float(b) * h * d * (2 * s_q + 2 * s_k) * elem
    return flops, total_bytes


def _resolve_peaks(args: argparse.Namespace, raw: dict) -> tuple[float, float, str]:
    if args.peak_tflops is not None and args.peak_gbps is not None:
        label = _DEVICE_PEAKS[args.device].label if args.device in _DEVICE_PEAKS else "custom"
        return args.peak_tflops, args.peak_gbps, label
    if args.device:
        key = args.device.lower()
        if key not in _DEVICE_PEAKS:
            raise SystemExit(  # noqa: TRY003  # tracked: #288
                f"unknown --device {args.device!r}; known: {', '.join(sorted(_DEVICE_PEAKS))}"
            )
        peak = _DEVICE_PEAKS[key]
        return peak.peak_tflops, peak.peak_gbps, peak.label
    detected = _detect_device_key(raw)
    if detected:
        peak = _DEVICE_PEAKS[detected]
        print(f"[roofline] auto-detected device: {peak.label}", file=sys.stderr)  # noqa: T201  # tracked: #288
        return peak.peak_tflops, peak.peak_gbps, peak.label
    raise SystemExit(  # noqa: TRY003  # tracked: #288
        "roofline needs --device <key> or --peak-tflops/--peak-gbps (could not "
        f"auto-detect device from trace deviceProperties); known --device values: "
        f"{', '.join(sorted(_DEVICE_PEAKS))}"
    )


def _roofline_row(metrics: _OpFlopsBytes, peak_tflops: float, peak_gbps: float) -> RooflineRow:
    seconds = metrics.gpu_time_us / 1_000_000.0
    achieved_tflops = metrics.flops / seconds / 1e12 if seconds > 0 else 0.0
    achieved_gbps = metrics.total_bytes / seconds / 1e9 if seconds > 0 else 0.0
    intensity = metrics.flops / metrics.total_bytes if metrics.total_bytes > 0 else 0.0
    ridge = (peak_tflops * 1e12) / (peak_gbps * 1e9) if peak_gbps > 0 else float("inf")
    bound = "compute" if intensity >= ridge else "memory"
    if bound == "compute":
        pct_peak = 100.0 * achieved_tflops / peak_tflops if peak_tflops > 0 else 0.0
    else:
        pct_peak = 100.0 * achieved_gbps / peak_gbps if peak_gbps > 0 else 0.0
    return RooflineRow(
        op=metrics.op_label,
        shape=metrics.shape,
        dtype=metrics.dtype,
        gpu_time_us=metrics.gpu_time_us,
        flops=metrics.flops,
        total_bytes=metrics.total_bytes,
        achieved_tflops=achieved_tflops,
        achieved_gbps=achieved_gbps,
        arithmetic_intensity=intensity,
        pct_of_peak=pct_peak,
        bound=bound,
    )


def _gemm_op_metrics(name: str, args: dict, dims: list, gpu_time_us: float) -> _OpFlopsBytes | None:
    shape = _shape_for_gemm_op(name, dims)
    if shape is None:
        return None
    m, n, k, batch = shape
    dtype = _dtype_for_gemm_op(name, args.get("Input type"))
    flops, total_bytes = _gemm_flops_bytes(m, n, k, batch, dtype)
    return _OpFlopsBytes(
        op_label=_GEMM_OPS[name],
        shape=_shape_label(m, n, k, batch),
        dtype=dtype,
        gpu_time_us=gpu_time_us,
        flops=flops,
        total_bytes=total_bytes,
    )


def _attention_op_metrics(
    name: str, args: dict, dims: list, gpu_time_us: float
) -> _OpFlopsBytes | None:
    dtype = _dtype_for_gemm_op(name, args.get("Input type"))
    fb = _attention_flops_bytes(dims, dtype)
    if fb is None:
        return None
    flops, total_bytes = fb
    b, h, s_q, d = dims[0]
    _b2, _h2, s_k, _d2 = dims[1]
    return _OpFlopsBytes(
        op_label=_ATTN_OPS[name],
        shape=f"B{b}H{h}Sq{s_q}Sk{s_k}D{d}",
        dtype=dtype,
        gpu_time_us=gpu_time_us,
        flops=flops,
        total_bytes=total_bytes,
    )


def _op_metrics_for_cpu_op(op: dict, gpu_time_us: float) -> _OpFlopsBytes | None:
    name = op.get("name", "")
    args = op.get("args") or {}
    dims = args.get("Input Dims")
    if not dims:
        return None
    if name in _GEMM_OPS:
        return _gemm_op_metrics(name, args, dims, gpu_time_us)
    if name in _ATTN_OPS:
        return _attention_op_metrics(name, args, dims, gpu_time_us)
    return None


def _extract_roofline_rows(
    index: TraceIndex, op_to_kernels: dict[int, list[dict]], peak_tflops: float, peak_gbps: float
) -> list[RooflineRow]:
    rows: list[RooflineRow] = []
    for i, op in enumerate(index.cpu_ops):
        if op.get("name", "") not in _GEMM_OPS and op.get("name", "") not in _ATTN_OPS:
            continue
        gpu_time_us = sum(float(kv.get("dur") or 0) for kv in op_to_kernels.get(i, ()))
        if gpu_time_us <= 0:
            continue
        metrics = _op_metrics_for_cpu_op(op, gpu_time_us)
        if metrics is not None:
            rows.append(_roofline_row(metrics, peak_tflops, peak_gbps))
    return rows


def _print_roofline(
    rows: list[RooflineRow], device_label: str, peak_tflops: float, peak_gbps: float
) -> None:
    peaks_line = (
        f"Device peaks: {device_label} -- {peak_tflops:.1f} TFLOP/s (dense), {peak_gbps:.0f} GB/s"
    )
    print(peaks_line)  # noqa: T201  # tracked: #288
    print()  # noqa: T201  # tracked: #288
    if not rows:
        print(  # noqa: T201  # tracked: #288
            "(no GEMM/attention ops with both shape and correlated GPU kernel time found -- "
            "run `certify` on this trace first)"
        )
        return
    print(  # noqa: T201  # tracked: #288
        f"{'Op':<12}{'Shape':<22}{'Dtype':<14}{'GPU time':>12}{'TFLOP/s':>10}"
        f"{'GB/s':>10}{'AI':>8}{'% peak':>9}  Bound"
    )
    print("-" * 104)  # noqa: T201  # tracked: #288
    for r in rows:
        print(  # noqa: T201  # tracked: #288
            f"{r.op:<12}{r.shape:<22}{r.dtype:<14}{_fmt_us(r.gpu_time_us):>12}"
            f"{r.achieved_tflops:>10.1f}{r.achieved_gbps:>10.0f}{r.arithmetic_intensity:>8.1f}"
            f"{r.pct_of_peak:>8.1f}%  {r.bound}"
        )


def _print_roofline_diagnosis(index: TraceIndex, rows: list[RooflineRow]) -> None:
    graph_launches = [
        ev
        for ev in (*index.runtime_launches, *index.kernels)
        if "graphlaunch" in ev.get("name", "").lower()
    ]
    if graph_launches:
        print(  # noqa: T201  # tracked: #288
            f"\nNote: {len(graph_launches)} hipGraphLaunch/cudaGraphLaunch replay events found -- "
            "per-kernel attribution for GEMMs launched inside a graph replay is unreliable. "
            "Re-profile in eager mode for trustworthy op-level roofline numbers."
        )
    if not rows:
        print(  # noqa: T201  # tracked: #288
            "\nNo hot kernels attributed to a GEMM/attention op -- if the host looks idle, "
            "re-profile under sustained load (see `certify`'s gpu_busy check)."
        )


def cmd_roofline(args: argparse.Namespace) -> None:
    """Achieved TFLOP/s, GB/s, arithmetic intensity, and bound class for GEMM/attention ops."""
    raw = _read_json_maybe_gz(args.trace)
    if not _is_chrome_trace(raw):
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"{args.trace} is not a raw Kineto/Chrome trace (no 'traceEvents' key). "
            "roofline needs the *.pt.trace.json(.gz) file, not a summarized prof.json."
        )
    peak_tflops, peak_gbps, device_label = _resolve_peaks(args, raw)
    index = _index_trace(raw)
    op_to_kernels = _build_op_to_kernels(index)
    rows = _extract_roofline_rows(index, op_to_kernels, peak_tflops, peak_gbps)
    rows.sort(key=lambda r: r.gpu_time_us, reverse=True)
    top = rows[: args.top] if args.top else rows
    _print_roofline(top, device_label, peak_tflops, peak_gbps)
    _print_roofline_diagnosis(index, rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:  # noqa: D103  # tracked: #288
    p = argparse.ArgumentParser(
        description="Torch profiler analysis toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    cap = sub.add_parser("capture", help="Capture a profile by loading VibeServeModel from main.py")
    cap.add_argument("--model-dir", required=True, help="Dir containing main.py")
    cap.add_argument("--weights-dir", default="/model", help="HF model dir (default: /model)")
    cap.add_argument("--output", required=True, help="Output JSON file")
    cap.add_argument("--num-iters", type=int, default=20)
    cap.add_argument("--warmup", type=int, default=3)
    cap.add_argument("--prompt", default="The capital of France is")
    cap.add_argument("--max-tokens", type=int, default=32)
    cap.add_argument("--device", default="cuda")
    cap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])

    srv = sub.add_parser(
        "capture-server", help="Capture a profile against a server with /admin/profile endpoints"
    )
    srv.add_argument("--url", required=True, help="Server base URL, e.g. http://localhost:8000")
    srv.add_argument("--output", required=True)
    srv.add_argument("--requests", type=int, default=10)
    srv.add_argument("--prompt", default="The capital of France is")
    srv.add_argument("--max-tokens", type=int, default=32)
    srv.add_argument("--timeout", type=float, default=300.0)

    for name, help_text in [
        ("tables", "List what's available in the profile"),
        ("kernels", "Top GPU kernels by self-CUDA time"),
        ("operators", "Top operators by self-CPU time"),
        ("cpu-overhead", "CPU vs GPU time ratio — detects launch-bound"),
        ("memory", "Memory operations"),
        ("summary", "All-in-one (runs certify first on a raw trace)"),
    ]:
        p_ = sub.add_parser(name, help=help_text)
        p_.add_argument(
            "report", help="Path to prof.json, or a raw *.pt.trace.json(.gz) Kineto/Chrome trace"
        )
        if name in ("kernels", "operators", "summary"):
            p_.add_argument("--top", type=int, default=15)

    cert = sub.add_parser(
        "certify", help="Structural validity check on a raw trace before trusting it"
    )
    cert.add_argument("trace", help="Path to a *.pt.trace.json(.gz) Kineto/Chrome trace")

    gemm = sub.add_parser(
        "gemm-shapes", help="Extract (M, N, K, dtype) GEMM demand from a raw trace"
    )
    gemm.add_argument("trace", help="Path to a *.pt.trace.json(.gz) Kineto/Chrome trace")
    gemm.add_argument("--top", type=int, default=20)
    gemm.add_argument("--out", help="Also write the ranked shapes as JSON (e.g. for GEMM tuning)")

    roof = sub.add_parser(
        "roofline",
        help="Achieved TFLOP/s, GB/s, arithmetic intensity, and bound class for GEMM/attention ops",
    )
    roof.add_argument("trace", help="Path to a *.pt.trace.json(.gz) Kineto/Chrome trace")
    roof.add_argument("--device", default=None, help=f"One of: {', '.join(sorted(_DEVICE_PEAKS))}")
    roof.add_argument("--peak-tflops", type=float, default=None, help="Explicit dense peak TFLOP/s")
    roof.add_argument(
        "--peak-gbps", type=float, default=None, help="Explicit peak HBM bandwidth, GB/s"
    )
    roof.add_argument("--top", type=int, default=20)

    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)

    {
        "capture": cmd_capture,
        "capture-server": cmd_capture_server,
        "tables": cmd_tables,
        "kernels": cmd_kernels,
        "operators": cmd_operators,
        "cpu-overhead": cmd_cpu_overhead,
        "memory": cmd_memory,
        "summary": cmd_summary,
        "certify": cmd_certify,
        "gemm-shapes": cmd_gemm_shapes,
        "roofline": cmd_roofline,
    }[args.command](args)


if __name__ == "__main__":
    main()
