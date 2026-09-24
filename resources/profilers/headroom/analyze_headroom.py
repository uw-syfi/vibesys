"""Kernel headroom report analysis toolkit — subcommand-based.

Unlike the nsys/torch profilers, this toolkit does not capture anything
itself. The *target* owns capture: its bundle ships a capture entry point
(conventionally ``benchmark/headroom_capture.sh <output.json>``, or whatever
``OBJECTIVE.md`` documents) that measures the candidate and writes a headroom
report. This toolkit analyzes that report: which kernels are farthest from the
hardware's speed-of-light, and whether the gap is kernel quality, eliminable
data movement, or missing fusion.

Usage:
    python analyze_headroom.py waterfall report.json
    python analyze_headroom.py top report.json [--top 15] [--klass movement]
    python analyze_headroom.py kernel report.json <name-substring>
    python analyze_headroom.py subgraphs report.json
    python analyze_headroom.py compare old.json new.json [--top 15]
    python analyze_headroom.py summary report.json [--top 10]

Report schema (headroom report v1) — the contract a capture entry point must
produce. Required:

    {
        "kernels": [
            {
                "kernel": str,                  # full kernel name (required)
                "observed_ms_step": float,      # measured time (required)
                # everything below is optional but drives richer analysis:
                "kind": str,                    # triton | gemm | attention | eager | ...
                "class": str,                   # quality | movement | library | artifact
                "calls_per_step": float,
                "flops_per_call": float, "bytes_per_call": float,
                "arithmetic_intensity": float,
                "sol_ms_step": float,           # roofline speed-of-light
                "opportunity_ms_step": float,   # recoverable time (ranking key)
                "source": [{"loc": str, "code": str}, ...],
                "notes": [str, ...],
            },
            ...
        ]
    }

Optional top-level fields, surfaced when present: ``buckets_ms_per_step``
(additive waterfall, e.g. observed / speed_of_light / movement_elimination /
fusion_in_graph / estimated_floor), ``subgraphs`` (per compiled-subgraph fusion
view), ``meta``, ``gpu_name``, ``gpu_spec_matched``, ``caveats``,
``definitions``, ``schema_version``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO


def _print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print user-facing command-line output."""
    if file is None:
        sys.stdout.write(sep.join(map(str, values)) + end)
        if flush:
            sys.stdout.flush()
    else:
        print(*values, sep=sep, end=end, file=file, flush=flush)


def _load(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text())
    except FileNotFoundError:
        sys.exit(f"headroom report not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"not valid JSON: {path} ({exc})")
    if not isinstance(payload, dict) or not isinstance(payload.get("kernels"), list):
        sys.exit(f"not a headroom report (missing top-level 'kernels' list): {path}")
    return payload


def _kernels(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [k for k in report["kernels"] if isinstance(k, dict) and "kernel" in k]
    return sorted(rows, key=_opportunity, reverse=True)


def _opportunity(row: dict[str, Any]) -> float:
    value = row.get("opportunity_ms_step")
    if isinstance(value, (int, float)):
        return float(value)
    observed = row.get("observed_ms_step")
    return float(observed) if isinstance(observed, (int, float)) else 0.0


def _fmt_ms(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) else "n/a"


def _print_header(report: dict[str, Any]) -> None:
    gpu = report.get("gpu_spec_matched") or report.get("gpu_name") or "unknown GPU"
    meta = report.get("meta") or {}
    parts = [f"gpu={gpu}"] + [f"{k}={v}" for k, v in meta.items()]
    _print("headroom report:", "  ".join(str(p) for p in parts))


def cmd_waterfall(ns: argparse.Namespace) -> None:
    """Additive headroom waterfall (buckets) plus optimality ratios."""
    report = _load(ns.report)
    _print_header(report)
    buckets = report.get("buckets_ms_per_step")
    if not isinstance(buckets, dict) or not buckets:
        _print("no 'buckets_ms_per_step' in this report; see `top` for per-kernel data")
        return
    observed = buckets.get("observed")
    total = float(observed) if isinstance(observed, (int, float)) and observed else None
    for name, value in buckets.items():
        share = (
            f"  ({value / total * 100:5.1f}%)" if total and isinstance(value, (int, float)) else ""
        )
        _print(f"  {name:<24} {_fmt_ms(value):>9} ms/step{share}")
    definitions = report.get("definitions")
    if isinstance(definitions, dict):
        _print("\nbucket definitions:")
        for name, text in definitions.items():
            _print(f"  {name}: {text}")


def cmd_top(ns: argparse.Namespace) -> None:
    """Kernels ranked by recoverable ms/step (opportunity, not raw time)."""
    report = _load(ns.report)
    _print_header(report)
    rows = _kernels(report)
    klass = getattr(ns, "klass", None)
    if klass:
        rows = [r for r in rows if r.get("class") == klass]
    for rank, row in enumerate(rows[: ns.top], 1):
        tags = "/".join(str(row[k]) for k in ("kind", "class") if row.get(k))
        calls = row.get("calls_per_step")
        calls_text = f"  x{calls:g}/step" if isinstance(calls, (int, float)) else ""
        _print(
            f"#{rank:<3} opportunity {_fmt_ms(row.get('opportunity_ms_step'))} ms/step"
            f"  observed {_fmt_ms(row.get('observed_ms_step'))}"
            f"  sol {_fmt_ms(row.get('sol_ms_step'))}{calls_text}"
            f"  [{tags or 'unclassified'}]"
        )
        _print(f"    kernel: {row['kernel']}")
        for src in (row.get("source") or [])[:3]:
            if isinstance(src, dict):
                _print(f"    source: {src.get('loc', '')}  {src.get('code', '')}")
    remainder = rows[ns.top :]
    if remainder:
        left = sum(_opportunity(r) for r in remainder)
        _print(f"( +{len(remainder)} more kernels, {left:.3f} ms/step opportunity )")


def cmd_kernel(ns: argparse.Namespace) -> None:
    """Full detail for every kernel whose name contains the given substring."""
    report = _load(ns.report)
    matches = [r for r in _kernels(report) if ns.name in r["kernel"]]
    if not matches:
        _print(f"no kernel name contains {ns.name!r}")
        return
    for row in matches:
        _print(json.dumps(row, indent=1))


def cmd_subgraphs(ns: argparse.Namespace) -> None:
    """Per-compiled-subgraph fusion view, when the report provides one."""
    report = _load(ns.report)
    subgraphs = report.get("subgraphs")
    if not isinstance(subgraphs, list) or not subgraphs:
        _print("no 'subgraphs' section in this report")
        return
    _print(json.dumps(subgraphs, indent=1))


def _print_bucket_deltas(old: dict[str, Any], new: dict[str, Any]) -> None:
    old_buckets = old.get("buckets_ms_per_step") or {}
    new_buckets = new.get("buckets_ms_per_step") or {}
    names = [k for k in new_buckets if k in old_buckets] if isinstance(old_buckets, dict) else []
    if not names:
        return
    _print("bucket deltas (new - old, negative is improvement):")
    for name in names:
        before, after = old_buckets[name], new_buckets[name]
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            _print(f"  {name:<24} {before:9.3f} -> {after:9.3f}   ({after - before:+.3f} ms/step)")


def _print_kernel_deltas(old: dict[str, Any], new: dict[str, Any], top: int) -> None:
    old_by_name = {r["kernel"]: r for r in _kernels(old)}
    deltas: list[tuple[float, str, float, float]] = []
    for row in _kernels(new):
        before_row = old_by_name.get(row["kernel"])
        after = row.get("observed_ms_step")
        before = before_row.get("observed_ms_step") if before_row else None
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            deltas.append((abs(after - before), row["kernel"], before, after))
    deltas.sort(reverse=True)
    if deltas:
        _print(f"\nbiggest per-kernel observed changes (top {top}):")
        for _, name, before, after in deltas[:top]:
            _print(f"  {before:8.3f} -> {after:8.3f}  ({after - before:+.3f})  {name}")
    only_new = [r["kernel"] for r in _kernels(new) if r["kernel"] not in old_by_name]
    only_old = [name for name in old_by_name if name not in {r["kernel"] for r in _kernels(new)}]
    for label, missing in (("new", only_new), ("old", only_old)):
        if missing:
            _print(f"\nkernels only in {label} report ({len(missing)}):")
            for name in missing[:top]:
                _print(f"  {name}")


def cmd_compare(ns: argparse.Namespace) -> None:
    """Bucket and per-kernel deltas between two reports (old -> new)."""
    old, new = _load(ns.old), _load(ns.new)
    _print_bucket_deltas(old, new)
    _print_kernel_deltas(old, new, ns.top)


def cmd_summary(ns: argparse.Namespace) -> None:
    """All-in-one: waterfall + top opportunities + caveats."""
    cmd_waterfall(ns)
    _print()
    cmd_top(ns)
    report = _load(ns.report)
    caveats = report.get("caveats")
    if isinstance(caveats, list) and caveats:
        _print("\ncaveats:")
        for caveat in caveats:
            _print(f"  - {caveat}")


def main(argv: list[str] | None = None) -> None:
    """Run the command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="analyze_headroom",
        description="Analyze a kernel headroom report (see module docstring for the schema).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    waterfall = sub.add_parser("waterfall", help="bucket waterfall + ratios")
    waterfall.add_argument("report")
    waterfall.set_defaults(fn=cmd_waterfall)

    top = sub.add_parser("top", help="kernels ranked by recoverable ms/step")
    top.add_argument("report")
    top.add_argument("--top", type=int, default=15)
    top.add_argument("--klass", default=None, help="filter by class (movement, quality, ...)")
    top.set_defaults(fn=cmd_top)

    kernel = sub.add_parser("kernel", help="full detail for kernels matching a substring")
    kernel.add_argument("report")
    kernel.add_argument("name")
    kernel.set_defaults(fn=cmd_kernel)

    subgraphs = sub.add_parser("subgraphs", help="per-subgraph fusion view")
    subgraphs.add_argument("report")
    subgraphs.set_defaults(fn=cmd_subgraphs)

    compare = sub.add_parser("compare", help="deltas between two reports")
    compare.add_argument("old")
    compare.add_argument("new")
    compare.add_argument("--top", type=int, default=15)
    compare.set_defaults(fn=cmd_compare)

    summary = sub.add_parser("summary", help="waterfall + top + caveats")
    summary.add_argument("report")
    summary.add_argument("--top", type=int, default=10)
    summary.add_argument("--klass", default=None)
    summary.set_defaults(fn=cmd_summary)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
