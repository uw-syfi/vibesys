#!/usr/bin/env python3
r"""Torch profiler analysis toolkit — subcommand-based.

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
    python analyze_torch_profile.py summary prof.json    # all-in-one

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
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from types import ModuleType

_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_MILLISECOND = 1000
_KERNEL_NAME_WIDTH = 58
_OPERATOR_NAME_WIDTH = 48
_CPU_BOUND_RATIO = 2.0
_GPU_BOUND_RATIO = 0.5
_MEMORY_NAME_WIDTH = 38


class ModelEntrypointNotFoundError(FileNotFoundError):
    """Report a model directory that does not contain its entrypoint."""

    @classmethod
    def for_path(cls, path: Path) -> ModelEntrypointNotFoundError:
        """Create a missing-main error with the expected model directory."""
        return cls(
            f"main.py not found at {path} — pass --model-dir pointing "
            "to the workspace root that contains main.py."
        )


class ModelInterfaceError(AttributeError):
    """Report a model entrypoint that does not follow the profiling contract."""

    @classmethod
    def missing_model_class(cls, path: Path) -> ModelInterfaceError:
        """Create an error for an entrypoint without VibeServeModel."""
        return cls(
            f"main.py at {path} does not export VibeServeModel. "
            "The accuracy-checker interface requires this symbol."
        )


class ProfileServerURLError(ValueError):
    """Report a profile server URL that cannot be used safely."""

    @classmethod
    def unsupported_scheme(cls) -> ProfileServerURLError:
        """Create an error when the profile server URL is not HTTP or HTTPS."""
        return cls("Profile server URL must use HTTP or HTTPS")


class ProfileServerContractError(RuntimeError):
    """Report a response that violates the profile server contract."""

    @classmethod
    def missing_events(cls) -> ProfileServerContractError:
        """Create an error when the profile response has no events key."""
        return cls(
            "/admin/profile/stop response missing 'events' key — "
            "is the server implementing the expected contract?"
        )


def _print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print user-facing command-line output."""
    if file is None:
        # lint-waiver: LW-008049 [T201]; This standalone CLI intentionally writes user-facing results to stdout.
        print(*values, sep=sep, end=end, flush=flush)  # noqa: T201
    else:
        print(*values, sep=sep, end=end, file=file, flush=flush)


# ---------------------------------------------------------------------------
# Capture: in-process (loads VibeServeModel from main.py)
# ---------------------------------------------------------------------------


def _load_main_module(model_dir: str) -> ModuleType:
    """Import ``main.py`` from *model_dir* and return the module.

    The agent's server always exports ``VibeServeModel`` from ``main.py``,
    matching the accuracy-checker contract.
    """
    main_path = Path(model_dir) / "main.py"
    if not main_path.is_file():
        raise ModelEntrypointNotFoundError.for_path(main_path)
    spec = importlib.util.spec_from_file_location("vs_main", str(main_path))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(main_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        if str(main_path.parent) in sys.path:
            sys.path.remove(str(main_path.parent))
    if not hasattr(module, "VibeServeModel"):
        raise ModelInterfaceError.missing_model_class(main_path)
    return module


def cmd_capture(args: argparse.Namespace) -> None:
    """Profile VibeServeModel.generate under torch.profiler, dump JSON."""
    torch_module = torch if torch is not None else importlib.import_module("torch")
    profiler_activity = torch_module.profiler.ProfilerActivity
    profile = torch_module.profiler.profile

    dtype = {
        "bfloat16": torch_module.bfloat16,
        "float16": torch_module.float16,
        "float32": torch_module.float32,
    }.get(args.dtype, torch_module.bfloat16)

    model_dir = args.model_dir
    weights_dir = args.weights_dir or "/model"

    _print(
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
        # lint-waiver: LW-008026 [PLC0415]; Transformers is an optional fallback used only when the loaded model does not provide a tokenizer.
        from transformers import AutoTokenizer  # noqa: PLC0415

        tokenizer = AutoTokenizer.from_pretrained(weights_dir)

    input_ids = tokenizer(args.prompt, return_tensors="pt").input_ids.to(args.device)

    # Warmup — first call compiles kernels, allocates KV cache, etc.
    _print(f"[capture] warmup ({args.warmup} iters)...", file=sys.stderr)
    for _ in range(args.warmup):
        with torch.no_grad():
            model.generate(input_ids=input_ids, max_new_tokens=args.max_tokens)
    torch.cuda.synchronize()

    _print(
        f"[capture] profiling ({args.num_iters} iters, max_new_tokens={args.max_tokens})...",
        file=sys.stderr,
    )
    t0 = time.time()
    with profile(
        activities=[profiler_activity.CPU, profiler_activity.CUDA],
        record_shapes=False,
        profile_memory=True,
        with_stack=False,
    ) as prof:
        with torch.no_grad():
            for _ in range(args.num_iters):
                model.generate(input_ids=input_ids, max_new_tokens=args.max_tokens)
        torch.cuda.synchronize()
    wall = time.time() - t0
    _print(f"[capture] elapsed {wall:.2f}s", file=sys.stderr)

    events_json = _summarize_prof(prof)
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
    _print(
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

    def _post(path: str, body: dict | None = None) -> dict:
        url = args.url.rstrip("/") + path
        if not url.startswith(("http:", "https:")):
            raise ProfileServerURLError.unsupported_scheme()
        data = json.dumps(body or {}).encode("utf-8")
        # lint-waiver: LW-008024 [S310]; This request uses a scheme-validated URL, and its opener accepts only HTTP and HTTPS handlers.
        req = urllib.request.Request(  # noqa: S310
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.OpenerDirector()
        opener.add_handler(urllib.request.HTTPHandler())
        opener.add_handler(urllib.request.HTTPSHandler())
        with opener.open(req, timeout=args.timeout) as resp:
            return json.loads(resp.read())

    _print(f"[capture-server] POST {args.url}/admin/profile/start", file=sys.stderr)
    _post("/admin/profile/start")

    _print(
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

    _print(f"[capture-server] POST {args.url}/admin/profile/stop", file=sys.stderr)
    result = _post("/admin/profile/stop")

    if "events" not in result:
        raise ProfileServerContractError.missing_events()

    result.setdefault("captured_at", datetime.now(UTC).isoformat())
    result.setdefault("mode", "server")
    result.setdefault("num_iters", args.requests)
    Path(args.output).write_text(json.dumps(result, indent=2))
    _print(f"[capture-server] wrote {args.output}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers: summarize torch.profiler events into JSON
# ---------------------------------------------------------------------------


def _summarize_prof(prof: object) -> dict:
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
        elif name.startswith(("aten::", "torch::")):
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
    torch = None


# ---------------------------------------------------------------------------
# Analysis subcommands
# ---------------------------------------------------------------------------


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _fmt_us(us: float) -> str:
    if us >= _MICROSECONDS_PER_SECOND:
        return f"{us / _MICROSECONDS_PER_SECOND:.2f} s"
    if us >= _MICROSECONDS_PER_MILLISECOND:
        return f"{us / _MICROSECONDS_PER_MILLISECOND:.2f} ms"
    return f"{us:.1f} us"


def cmd_kernels(args: argparse.Namespace) -> None:
    """Top GPU kernels by total self-CUDA time."""
    data = _load(args.report)
    kernels = [e for e in data["events"] if e["self_cuda_time_us"] > 0]
    kernels.sort(key=lambda e: e["self_cuda_time_us"], reverse=True)
    total = data["total_cuda_time_us"] or 1.0
    _print(f"Total self-CUDA time: {_fmt_us(total)}")
    _print()
    _print(f"{'Name':<60}{'Self CUDA':>14}{'% of total':>12}{'Count':>10}")
    _print("-" * 96)
    for ev in kernels[: args.top]:
        pct = 100.0 * ev["self_cuda_time_us"] / total
        name = ev["name"]
        if len(name) > _KERNEL_NAME_WIDTH:
            name = name[:55] + "..."
        _print(f"{name:<60}{_fmt_us(ev['self_cuda_time_us']):>14}{pct:>11.1f}%{ev['count']:>10}")


def cmd_operators(args: argparse.Namespace) -> None:
    """Top operators (aten::*, torch::*) by CPU time."""
    data = _load(args.report)
    ops = [e for e in data["events"] if e["category"] == "operator"]
    ops.sort(key=lambda e: e["self_cpu_time_us"], reverse=True)
    total_cpu = data["total_cpu_time_us"] or 1.0
    _print(f"Total self-CPU time: {_fmt_us(total_cpu)}")
    _print()
    _print(f"{'Operator':<50}{'Self CPU':>14}{'CUDA time':>14}{'% CPU':>10}{'Count':>10}")
    _print("-" * 98)
    for ev in ops[: args.top]:
        pct = 100.0 * ev["self_cpu_time_us"] / total_cpu
        name = ev["name"]
        if len(name) > _OPERATOR_NAME_WIDTH:
            name = name[:45] + "..."
        _print(
            f"{name:<50}{_fmt_us(ev['self_cpu_time_us']):>14}"
            f"{_fmt_us(ev['cuda_time_us']):>14}{pct:>9.1f}%{ev['count']:>10}"
        )


def cmd_cpu_overhead(args: argparse.Namespace) -> None:
    """CPU vs GPU time breakdown — detects launch-bound scenarios."""
    data = _load(args.report)
    total_cpu = data["total_cpu_time_us"]
    total_cuda = data["total_cuda_time_us"]
    ratio = total_cpu / total_cuda if total_cuda else float("inf")
    _print(f"Total self-CPU time:  {_fmt_us(total_cpu)}")
    _print(f"Total self-CUDA time: {_fmt_us(total_cuda)}")
    _print(f"CPU/CUDA ratio:       {ratio:.2f}x")
    if ratio > _CPU_BOUND_RATIO:
        _print()
        _print(
            "Interpretation: CPU time dominates (>2x GPU). Likely launch-bound"
            " — consider CUDA graphs, fewer kernels, or larger batches."
        )
    elif ratio < _GPU_BOUND_RATIO:
        _print()
        _print(
            "Interpretation: GPU time dominates (<0.5x CPU). Compute-bound —"
            " focus on kernel fusion, flash attention, better algorithms."
        )
    else:
        _print()
        _print(
            "Interpretation: CPU and GPU roughly balanced. Both axes may benefit from optimization."
        )


def cmd_memory(args: argparse.Namespace) -> None:
    """Memory allocation / transfer events."""
    data = _load(args.report)
    mem = [e for e in data["events"] if e["category"] == "memory"]
    mem.sort(key=lambda e: e["cuda_time_us"] + e["cpu_time_us"], reverse=True)
    if not mem:
        _print("(no memory events recorded)")
        return
    _print(f"{'Operation':<40}{'Total CUDA':>14}{'Total CPU':>14}{'Count':>10}")
    _print("-" * 78)
    for ev in mem:
        name = ev["name"]
        if len(name) > _MEMORY_NAME_WIDTH:
            name = name[:35] + "..."
        _print(
            f"{name:<40}{_fmt_us(ev['cuda_time_us']):>14}"
            f"{_fmt_us(ev['cpu_time_us']):>14}{ev['count']:>10}"
        )


def cmd_summary(args: argparse.Namespace) -> None:
    """All-in-one: overhead + kernels + operators + memory."""
    args.top = getattr(args, "top", 15)
    _print("=" * 80)
    _print("  TORCH PROFILER SUMMARY")
    _print("=" * 80)
    data = _load(args.report)
    _print(f"\nCaptured: {data.get('captured_at', '?')}")
    _print(f"Mode:     {data.get('mode', '?')}")
    _print(f"Device:   {data.get('device', '?')} ({data.get('dtype', '?')})")
    if "wall_time_sec" in data:
        _print(f"Wall:     {data['wall_time_sec']:.2f}s over {data.get('num_iters', '?')} iters")
    _print("\n## CPU / GPU Overhead\n")
    cmd_cpu_overhead(args)
    _print("\n## Top GPU Kernels\n")
    cmd_kernels(args)
    _print("\n## Top Operators\n")
    cmd_operators(args)
    _print("\n## Memory Operations\n")
    cmd_memory(args)


def cmd_tables(args: argparse.Namespace) -> None:
    """List available analyses (mirrors nsys 'tables' convention)."""
    data = _load(args.report)
    categories: dict[str, int] = {}
    for ev in data["events"]:
        categories[ev["category"]] = categories.get(ev["category"], 0) + 1
    _print(f"Captured:   {data.get('captured_at', '?')}")
    _print(f"Mode:       {data.get('mode', '?')}")
    _print(f"Num events: {data.get('num_events', len(data.get('events', [])))}")
    _print(f"Total CUDA: {_fmt_us(data.get('total_cuda_time_us', 0))}")
    _print(f"Total CPU:  {_fmt_us(data.get('total_cpu_time_us', 0))}")
    _print("\nEvent categories:")
    for cat, n in sorted(categories.items(), key=lambda kv: -kv[1]):
        _print(f"  {cat:<10} {n:>6} events")
    _print("\nAvailable subcommands: kernels, operators, cpu-overhead, memory, summary")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the command-line entry point."""
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
        ("summary", "All-in-one"),
    ]:
        p_ = sub.add_parser(name, help=help_text)
        p_.add_argument("report", help="Path to prof.json")
        if name in ("kernels", "operators", "summary"):
            p_.add_argument("--top", type=int, default=15)

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
    }[args.command](args)


if __name__ == "__main__":
    main()
