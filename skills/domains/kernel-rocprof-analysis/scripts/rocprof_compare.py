#!/usr/bin/env python3
"""A/B comparison of two rocprofv3 captures of the same kind.

Usage (through ``analyze.py compare``, or this file directly):
    rocprof_compare.py timeline <a_report> <b_report> [--top 15] [--window all]
    rocprof_compare.py counters --arch gfx90a --a <dir> [<dir> ...] --b <dir> [<dir> ...]

``timeline`` diffs per-family and per-kernel GPU time between two system-trace
captures; ``counters`` diffs per-kernel derived counter metrics between two sets
of PMC pass directories. Every delta is ``b - a``: negative means ``b`` spent
less time, or had a lower rate, than ``a``. A comparison of profiles explains a
difference; it never replaces the task's benchmark verdict.
"""

from __future__ import annotations

import argparse

import rocprof_counters
import rocprof_trace
from analysis_errors import analysis_failed

DEFAULT_TOP = 15
COUNTER_METRIC_FIELDS = (
    "l2_hit_rate_pct",
    "achieved_bw_gb_s",
    "gpu_busy_pct",
    "mfma_issue_rate",
    "mfma_busy_fraction",
    "lds_bank_conflict_rate_pct",
)


def delta_rows(
    totals_a: dict[str, float], totals_b: dict[str, float]
) -> list[tuple[str, float, float, float]]:
    """(name, a_value, b_value, b-a) for every name on either side, by |delta| descending.

    Antisymmetric: swapping the inputs negates every delta and swaps the two values.
    Ties keep name order, so the ranking is deterministic.
    """
    names = sorted(set(totals_a) | set(totals_b))
    rows = [(name, totals_a.get(name, 0.0), totals_b.get(name, 0.0)) for name in names]
    return sorted(
        ((name, va, vb, vb - va) for name, va, vb in rows),
        key=lambda row: abs(row[3]),
        reverse=True,
    )


def compare_timeline(report_a: str, report_b: str, *, top: int, window: str | None) -> str:
    """Family and kernel GPU-time deltas between two timeline captures."""
    kernels_a = rocprof_trace.kernel_time_totals(report_a, window=window)
    kernels_b = rocprof_trace.kernel_time_totals(report_b, window=window)
    if not kernels_a or not kernels_b:
        empty = report_a if not kernels_a else report_b
        raise analysis_failed(f"error: no kernel data in {empty}; nothing to compare")
    families_a = rocprof_trace.family_time_totals(report_a, window=window)
    families_b = rocprof_trace.family_time_totals(report_b, window=window)

    lines = ["Family time deltas (b - a, nanoseconds):"]
    for name, va, vb, delta in delta_rows(families_a, families_b)[:top]:
        lines.append(f"  {name:<28} a={va:>14,.0f}  b={vb:>14,.0f}  delta={delta:>+14,.0f}")
    lines.append("\nTop kernel time deltas (b - a, nanoseconds):")
    for name, va, vb, delta in delta_rows(kernels_a, kernels_b)[:top]:
        lines.append(f"  {name[:60]:<60} a={va:>14,.0f}  b={vb:>14,.0f}  delta={delta:>+14,.0f}")

    new_kernels = sorted(set(kernels_b) - set(kernels_a))
    removed_kernels = sorted(set(kernels_a) - set(kernels_b))
    if new_kernels:
        lines.append(f"\nNew kernels in b (not in a): {len(new_kernels)}")
        lines.extend(f"  + {name[:80]}" for name in new_kernels[:top])
    if removed_kernels:
        lines.append(f"\nKernels removed in b (present in a): {len(removed_kernels)}")
        lines.extend(f"  - {name[:80]}" for name in removed_kernels[:top])
    return "\n".join(lines)


def _fmt_optional(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def compare_counters(dirs_a: list[str], dirs_b: list[str], *, arch: str) -> str:
    """Per-kernel derived counter metric deltas between two sets of PMC pass directories."""
    metrics_a = rocprof_counters.kernel_metrics_by_name(dirs_a, arch=arch)
    metrics_b = rocprof_counters.kernel_metrics_by_name(dirs_b, arch=arch)
    if not metrics_a or not metrics_b:
        empty = dirs_a if not metrics_a else dirs_b
        raise analysis_failed(f"error: no counter data under {empty}; nothing to compare")
    lines = [f"Counter metric deltas for {arch} (b - a):"]
    for name in sorted(set(metrics_a) | set(metrics_b)):
        metric_a, metric_b = metrics_a.get(name), metrics_b.get(name)
        lines.append(f"\n{name[:80]}")
        for field_name in COUNTER_METRIC_FIELDS:
            va = getattr(metric_a, field_name, None) if metric_a else None
            vb = getattr(metric_b, field_name, None) if metric_b else None
            delta = (vb - va) if va is not None and vb is not None else None
            lines.append(
                f"  {field_name:<28} a={_fmt_optional(va)}  b={_fmt_optional(vb)}  "
                f"delta={_fmt_optional(delta, signed=True)}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """Parse the comparison kind and print its deltas."""
    parser = argparse.ArgumentParser(prog="rocprof_compare.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="kind", required=True)

    timeline = sub.add_parser("timeline", help="family and kernel GPU-time deltas")
    timeline.add_argument("a", help="baseline timeline capture directory or trace file")
    timeline.add_argument("b", help="candidate timeline capture directory or trace file")
    timeline.add_argument("--top", type=int, default=DEFAULT_TOP)
    timeline.add_argument("--window", default=None, help="'all' (default) or 'start_s:end_s'")

    counters = sub.add_parser("counters", help="per-kernel derived counter metric deltas")
    counters.add_argument("--arch", required=True, help="e.g. gfx90a, mi210, gfx942, mi300x")
    counters.add_argument("--a", nargs="+", required=True, help="baseline PMC pass directories")
    counters.add_argument("--b", nargs="+", required=True, help="candidate PMC pass directories")

    ns = parser.parse_args(argv)
    if ns.kind == "timeline":
        print(compare_timeline(ns.a, ns.b, top=ns.top, window=ns.window))
    else:
        print(compare_counters(ns.a, ns.b, arch=ns.arch))


if __name__ == "__main__":
    main()
