#!/usr/bin/env python3
"""rocprofv3 trace analysis toolkit — subcommand-based.

The AMD analog of ``analyze_nsys.py``: each subcommand queries one aspect of
a rocprofv3 capture. ``<report>`` may be a rocprofv3 output directory
(recursively scanned, including the per-``<hostname>/<pid>`` subdirectories
rocprofv3 writes), or a single trace/stats/JSON/rocpd-SQLite file.

Supported inputs, in preference order:

- CSV trace files: ``*_kernel_trace.csv``, ``*_hip_api_trace.csv``,
  ``*_memory_copy_trace.csv`` — per-event rows with timestamps. Needed for
  idle-gap, launch-bound-correlation, graph-attribution, and host-idle
  analysis. Real captures can be hundreds of MB with millions of rows
  (``--hip-runtime-trace`` in particular), so these are read with a
  streaming ``csv.reader`` that aggregates on the fly instead of
  materializing every row; see "Streaming trace loaders" below.
- CSV stats files: ``*_kernel_stats.csv``, ``*_hip_api_stats.csv`` —
  pre-aggregated (name, calls, total/avg/min/max duration). Enough for
  ``kernels``/``families``/``cpu_overhead`` totals, but not for anything that
  needs per-event timestamps. These are small (KB-sized) and read whole.
- ``*_domain_stats.csv`` / ``*_agent_info.csv`` — used by ``files``.
- rocprofv3 JSON output (``--output-format json``) — best-effort support for
  the documented ``buffer_records`` (``kernel_dispatch``, ``hip_api``,
  ``memory_copy``) shape, used only when no matching CSV is present. Capture
  guidance recommends CSV-only output (JSON balloons to GB-scale), so this
  path is not optimized for huge files.
- rocpd SQLite (``*.db``, ROCm 7) — only wired up for ``query``; every other
  subcommand explains that it needs CSV or JSON instead.

Column names vary across ROCm/rocprofv3 versions, so every reader resolves a
handful of candidate names per logical field instead of assuming one exact
header.

Streaming trace loaders:

``_get_kernel_bundle``/``_get_api_bundle``/``_get_memcpy_bundle`` each parse
their CSV family exactly once per report (memoized by file path + size +
mtime in ``_BUNDLE_CACHE``), producing a small aggregate ``*Bundle``
dataclass rather than a Python list of per-event dicts. ``summary`` invokes
every subcommand in one process, and without this cache a multi-hundred-MB
trace file would be re-parsed once per section that touches it. The bundles
keep only what downstream analysis needs: per-name aggregates, per
(agent, queue) merged-interval inputs, and per-correlation-id kernel
durations for HIP-API matching — not a dict per raw event.

Usage:
    python analyze_rocprof.py files <report>
    python analyze_rocprof.py kernels <report> [--top N]
    python analyze_rocprof.py families <report>
    python analyze_rocprof.py idle_gaps <report> [--top N]
    python analyze_rocprof.py cpu_overhead <report>
    python analyze_rocprof.py memory <report>
    python analyze_rocprof.py graphs <report>
    python analyze_rocprof.py host_idle <report>
    python analyze_rocprof.py query <report> "<sql>"
    python analyze_rocprof.py summary <report> [--top N]
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Small numeric / string helpers
# ---------------------------------------------------------------------------

_NS_PER_SEC = 1e9
_NS_PER_MS = 1e6
_NS_PER_US = 1e3
_BYTES_PER_GB = 1e9
_MAX_NAMESPACE_PARTS = 2


def _get(row: dict, cols: tuple[str, ...]) -> str | None:
    """Return the first non-empty value among candidate column names.

    Tries exact keys first, then falls back to a case-insensitive match, so
    callers stay tolerant of the column-naming variants across rocprofv3
    versions.
    """
    for c in cols:
        v = row.get(c)
        if v not in (None, ""):
            return v
    lower = {k.lower(): v for k, v in row.items()}
    for c in cols:
        v = lower.get(c.lower())
        if v not in (None, ""):
            return v
    return None


def _numf(v: object, default: float = 0.0) -> float:
    """Parse a value as a float, tolerating thousands separators and blanks."""
    if v is None or v == "":
        return default
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        return default


def _short_name(raw: str) -> str:
    """Shorten a templated/mangled kernel or API name for display."""
    result: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            result.append(ch)
    name = "".join(result).strip()
    parts = name.split("::")
    if len(parts) > _MAX_NAMESPACE_PARTS:
        name = "::".join(parts[-_MAX_NAMESPACE_PARTS:])
    return name or raw


def _truncate(name: str, width: int) -> str:
    if len(name) <= width:
        return name
    return name[: max(0, width - 3)] + "..."


def _fmt_ns(ns: float) -> str:
    if ns >= _NS_PER_SEC:
        return f"{ns / _NS_PER_SEC:.2f}s"
    if ns >= _NS_PER_MS:
        return f"{ns / _NS_PER_MS:.2f}ms"
    if ns >= _NS_PER_US:
        return f"{ns / _NS_PER_US:.2f}us"
    return f"{ns:.0f}ns"


_DIRECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hosttodevice", "HtoD"),
    ("h2d", "HtoD"),
    ("devicetohost", "DtoH"),
    ("d2h", "DtoH"),
    ("devicetodevice", "DtoD"),
    ("d2d", "DtoD"),
    ("hosttohost", "HtoH"),
    ("h2h", "HtoH"),
)


def _normalize_direction(raw: str) -> str:
    lraw = raw.lower().replace("_", "").replace(" ", "")
    for pat, label in _DIRECTION_PATTERNS:
        if pat in lraw:
            return label
    return raw or "?"


# ---------------------------------------------------------------------------
# Kernel library family classification
# ---------------------------------------------------------------------------
#
# Best-effort, name-only classification: rocprofv3 traces carry only the
# dispatched kernel symbol name, not which library issued it. Rules are
# ordered — the first match wins — and checked against the lowercased name.

_FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AITER (asm/ck)",
        ("aiter", "fmha_fwd_v3", "fmha_bwd_v3", "ck_moe_stage", "moe_ck_", "asm_pa_", "asm_gemm"),
    ),
    (
        "Composable Kernel (ck::/ck_tile)",
        ("ck::", "ck_tile::", "devicegemm", "devicebatchedgemm", "devicegroupedgemm", "devicemoe"),
    ),
    ("hipBLASLt / Tensile (Cijk_*)", ("cijk_", "hipblaslt")),
    ("rocBLAS", ("rocblas_", "rocblas::")),
    ("MIOpen", ("miopen", "naive_conv", "gcnasmconv", "sp3asmconv")),
    ("RCCL", ("nccl", "rccl")),
    (
        "vLLM/SGLang custom ops",
        (
            "paged_attention",
            "reshape_and_cache",
            "rms_norm",
            "silu_and_mul",
            "gelu_and_mul",
            "moe_align_block_size",
            "fused_add_rms_norm",
            "rotary_embedding",
            "awq_gemm",
            "awq_dequantize",
            "gptq_gemm",
            "marlin_gemm",
            "topk_softmax",
            "scaled_fp8_quant",
            "per_token_group_quant",
            "moe_sum",
            "grouped_topk",
            "cutlass_scaled_mm",
            "wvsplitk",
        ),
    ),
    ("Triton (JIT)", ("triton_poi_fused", "triton_red_fused", "triton_per_fused", "triton_")),
    (
        "PyTorch native (at::native)",
        (
            "at::native",
            "aten::",
            "torch::",
            "vectorized_elementwise_kernel",
            "unrolled_elementwise_kernel",
            "elementwise_kernel",
            "reduce_kernel",
            "catarraybatchedcopy",
            "index_elementwise_kernel",
            "distribution_elementwise_grid_stride_kernel",
            "softmax_kernel",
            "layer_norm_kernel",
            "topk_kernel",
        ),
    ),
)

FALLBACK_FAMILIES = frozenset({"Triton (JIT)", "PyTorch native (at::native)", "other"})
_GEMM_ATTN_RE = re.compile(
    r"gemm|matmul|attn|attention|sdpa|flash|fmha|scaled_dot|conv", re.IGNORECASE
)
_FALLBACK_FAMILY_SHARE_THRESHOLD = 5.0


def _looks_like_triton_kernel(name: str) -> bool:
    """Structural fallback for a Triton JIT kernel with no ``triton_*`` prefix.

    Only kernels rocprofv3 renamed via torch.inductor carry a ``triton_``
    prefix (the ``_FAMILY_RULES`` entry above). A kernel compiled directly
    from a ``@triton.jit`` function (vLLM/SGLang's own ops — paged-attention
    helpers, MoE routing, and linear-attention/GDN kernels such as
    ``fused_recurrent_gated_delta_rule_packed_decode_kernel`` or
    ``chunk_gated_delta_rule_fwd_kernel_h_blockdim64``) keeps its Python
    function name verbatim instead: snake_case, usually leading-underscore
    or ``_kernel``-suffixed by convention, with none of the C++ symbol shape
    (``::`` namespaces, ``<...>`` template args, or an un-demangled Itanium
    ``_Z...`` mangled prefix) that a hand-written HIP/CK kernel has.
    """
    if "::" in name or "<" in name or name.startswith(("_Z", "__")):
        return False
    lname = name.lower()
    return "_kernel" in lname or lname.startswith("_")


def _classify_family(name: str) -> str:
    """Classify a kernel name into a library family (best-effort, name-only)."""
    lname = name.lower()
    for family, subs in _FAMILY_RULES:
        if any(s in lname for s in subs):
            return family
    if _looks_like_triton_kernel(name):
        return "Triton (JIT)"
    return "other"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredReport:
    """The rocprofv3 output files found under one report path, by kind."""

    root: Path
    kernel_trace: list[Path] = field(default_factory=list)
    kernel_stats: list[Path] = field(default_factory=list)
    hip_api_trace: list[Path] = field(default_factory=list)
    hip_api_stats: list[Path] = field(default_factory=list)
    memory_copy_trace: list[Path] = field(default_factory=list)
    memory_copy_stats: list[Path] = field(default_factory=list)
    domain_stats: list[Path] = field(default_factory=list)
    agent_info: list[Path] = field(default_factory=list)
    other_csv: list[Path] = field(default_factory=list)
    json_files: list[Path] = field(default_factory=list)
    db_files: list[Path] = field(default_factory=list)


_SUFFIX_BUCKETS: tuple[tuple[str, str], ...] = (
    ("_kernel_trace.csv", "kernel_trace"),
    ("_kernel_stats.csv", "kernel_stats"),
    ("_hip_api_trace.csv", "hip_api_trace"),
    ("_hip_api_stats.csv", "hip_api_stats"),
    ("_memory_copy_trace.csv", "memory_copy_trace"),
    ("_memory_copy_stats.csv", "memory_copy_stats"),
    ("_domain_stats.csv", "domain_stats"),
    ("_agent_info.csv", "agent_info"),
)


def _bucket_for(name: str) -> str:
    lower = name.lower()
    for suffix, bucket in _SUFFIX_BUCKETS:
        if lower.endswith(suffix):
            return bucket
    if lower.endswith(".csv"):
        return "other_csv"
    if lower.endswith(".json"):
        return "json_files"
    if lower.endswith(".db"):
        return "db_files"
    return ""


def discover(report: str) -> DiscoveredReport:
    """Resolve *report* (a directory or a single file) into its output files."""
    p = Path(report)
    if p.is_dir():
        candidates = sorted(x for x in p.rglob("*") if x.is_file())
        disc = DiscoveredReport(root=p)
    elif p.is_file():
        candidates = [p]
        disc = DiscoveredReport(root=p.parent)
    else:
        raise FileNotFoundError(f"report path not found: {report}")  # noqa: TRY003
    for f in candidates:
        bucket = _bucket_for(f.name)
        if bucket:
            getattr(disc, bucket).append(f)
    return disc


def _process_dirs(disc: DiscoveredReport) -> list[Path]:
    dirs: set[Path] = set()
    for lst in (
        disc.kernel_trace,
        disc.hip_api_trace,
        disc.memory_copy_trace,
        disc.kernel_stats,
        disc.hip_api_stats,
    ):
        dirs.update(f.parent for f in lst)
    return sorted(dirs)


def _describe_process_dir(d: Path, root: Path) -> str:
    try:
        rel = d.relative_to(root)
    except ValueError:
        rel = d
    parts = rel.parts
    if parts and parts[-1].isdigit():
        host = parts[-2] if len(parts) >= _MAX_NAMESPACE_PARTS else "?"
        return f"{rel}  (pid={parts[-1]}, host={host})"
    return str(rel) if str(rel) != "." else "(report root)"


def _csv_row_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            n = sum(1 for _ in csv.reader(f))
    except OSError:
        return 0
    return max(0, n - 1)


# ---------------------------------------------------------------------------
# Column-name candidates (shared by the streaming and small-file readers)
# ---------------------------------------------------------------------------

_KERNEL_NAME_COLS = ("Kernel_Name", "Name", "KernelName")
_START_COLS = ("Start_Timestamp", "Start_Timestamp_ns", "Start (ns)", "Start")
_END_COLS = ("End_Timestamp", "End_Timestamp_ns", "End (ns)", "End")
_DUR_COLS = ("Duration_Ns", "DurationNs", "Duration")
_AGENT_COLS = ("Agent_Id", "Device_Id", "gpu-id", "Agent")
_QUEUE_COLS = ("Queue_Id", "Queue")
_CORR_COLS = ("Correlation_Id", "Corr_Id")
_PID_COLS = ("Pid", "PID", "Process_Id")

_API_NAME_COLS = ("Name", "Function")

_STATS_NAME_COLS = ("Name", "Kernel_Name", "Function")
_STATS_CALLS_COLS = ("Calls", "Count")
_STATS_TOTAL_COLS = (
    "TotalDurationNs",
    "Total_Duration_Ns",
    "TotalDuration(ns)",
    "DurationNs",
    "Sum_Ns",
)
_STATS_MIN_COLS = ("MinNs", "Min_Ns", "MinDuration(ns)")
_STATS_MAX_COLS = ("MaxNs", "Max_Ns", "MaxDuration(ns)")

_MEMCPY_DIR_COLS = ("Direction", "Memory_Copy_Kind", "Copy_Kind", "Kind")
_MEMCPY_BYTES_COLS = ("Bytes", "Size", "Num_Bytes", "Copy_Size")

_AGENT_NAME_COLS = ("Product_Name", "Name", "Model_Name")
_AGENT_GFX_COLS = ("Gfx_Target_Version", "Gfx_Arch")
_AGENT_TYPE_COLS = ("Type", "Agent_Type")
_AGENT_ID_COLS = ("Agent_Id", "Node_Id")


def _read_csv_dicts(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _stats_rows_from_csv(path: Path) -> list[dict]:
    rows = []
    for r in _read_csv_dicts(path):
        name = (_get(r, _STATS_NAME_COLS) or "?").strip()
        total_ns = _numf(_get(r, _STATS_TOTAL_COLS))
        calls = int(_numf(_get(r, _STATS_CALLS_COLS)))
        min_ns = _numf(_get(r, _STATS_MIN_COLS), total_ns)
        max_ns = _numf(_get(r, _STATS_MAX_COLS), total_ns)
        rows.append(
            {"name": name, "calls": calls, "total_ns": total_ns, "min_ns": min_ns, "max_ns": max_ns}
        )
    return rows


def _agent_rows_from_csv(path: Path) -> list[dict]:
    return [
        {
            "agent": _get(r, _AGENT_ID_COLS) or "?",
            "name": _get(r, _AGENT_NAME_COLS) or "?",
            "gfx": _get(r, _AGENT_GFX_COLS) or "?",
            "type": _get(r, _AGENT_TYPE_COLS) or "?",
        }
        for r in _read_csv_dicts(path)
    ]


def _aggregate_stats(rows: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        entry = agg.setdefault(
            r["name"], {"calls": 0, "total_ns": 0.0, "min_ns": float("inf"), "max_ns": 0.0}
        )
        entry["calls"] += r["calls"]
        entry["total_ns"] += r["total_ns"]
        entry["min_ns"] = min(entry["min_ns"], r["min_ns"])
        entry["max_ns"] = max(entry["max_ns"], r["max_ns"])
    return agg


def _load_agents(disc: DiscoveredReport) -> list[dict]:
    seen: dict[str, dict] = {}
    for f in disc.agent_info:
        for a in _agent_rows_from_csv(f):
            seen.setdefault(a["agent"], a)
    return [seen[k] for k in sorted(seen)]


def _load_api_stats(disc: DiscoveredReport) -> dict[str, dict]:
    rows = [row for f in disc.hip_api_stats for row in _stats_rows_from_csv(f)]
    return _aggregate_stats(rows)


# ---------------------------------------------------------------------------
# Interval merging (shared by idle-gap and host-idle busy/idle accounting)
# ---------------------------------------------------------------------------


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    """Sort and merge overlapping/touching ``(start, end)`` intervals."""
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _union_duration(events: list[dict]) -> float:
    """Total wall time covered by *events* (each a dict with start/end ns), no double-counting."""
    if not events:
        return 0.0
    merged = _merge_intervals([(e["start_ns"], e["end_ns"]) for e in events])
    return sum(e - s for s, e in merged)


# ---------------------------------------------------------------------------
# Streaming CSV trace bundles
# ---------------------------------------------------------------------------
#
# Real captures put millions of rows in *_hip_api_trace.csv (see the module
# docstring). These loaders make exactly one streaming pass per file with a
# plain ``csv.reader`` (not ``DictReader``, which builds a full-width dict
# per row even though only a handful of fields are used) and keep only
# per-name aggregates plus the small per-event subsets later analysis
# actually needs (grouped kernel intervals, launch-call correlation ids) —
# never a dict per raw row.


def _resolve_col(header: list[str], cols: tuple[str, ...]) -> int:
    """Return the index of the first matching candidate column, or -1."""
    for c in cols:
        if c in header:
            return header.index(c)
    lower = [h.lower() for h in header]
    for c in cols:
        try:
            return lower.index(c.lower())
        except ValueError:
            continue
    return -1


def _at(row: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _new_agg_entry() -> dict:
    return {"calls": 0, "total_ns": 0.0, "min_ns": float("inf"), "max_ns": 0.0}


def _bump_agg(agg: dict[str, dict], name: str, dur_ns: float) -> None:
    entry = agg.get(name)
    if entry is None:
        entry = _new_agg_entry()
        agg[name] = entry
    entry["calls"] += 1
    entry["total_ns"] += dur_ns
    entry["min_ns"] = min(entry["min_ns"], dur_ns)
    entry["max_ns"] = max(entry["max_ns"], dur_ns)


@dataclass
class KernelBundle:
    """One streaming pass over ``*_kernel_trace.csv`` (or a JSON/stats fallback)."""

    by_name: dict[str, dict] = field(default_factory=dict)
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = field(default_factory=dict)
    dur_by_corr: dict[str, list[float]] = field(default_factory=dict)
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0
    source: str = ""


@dataclass
class ApiBundle:
    """One streaming pass over ``*_hip_api_trace.csv`` (or a JSON fallback)."""

    by_name: dict[str, dict] = field(default_factory=dict)
    direct_launches: list[tuple[str, float]] = field(default_factory=list)
    graph_launches: list[tuple[str, float]] = field(default_factory=list)
    sync_count: int = 0
    sync_total_ns: float = 0.0
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0
    source: str = ""


@dataclass
class MemcpyBundle:
    """One streaming pass over ``*_memory_copy_trace.csv`` (or a JSON fallback)."""

    by_dir: dict[str, dict] = field(default_factory=dict)
    bytes_available: bool = False
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0


def _iter_csv_reader(path: Path) -> Iterator[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return
        yield header
        yield from reader


def _build_kernel_bundle_from_csv(files: list[Path]) -> KernelBundle:
    by_name: dict[str, dict] = {}
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    dur_by_corr: dict[str, list[float]] = defaultdict(list)
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        rows = _iter_csv_reader(path)
        header = next(rows, None)
        if header is None:
            continue
        i_name = _resolve_col(header, _KERNEL_NAME_COLS)
        i_start = _resolve_col(header, _START_COLS)
        i_end = _resolve_col(header, _END_COLS)
        i_dur = _resolve_col(header, _DUR_COLS)
        i_agent = _resolve_col(header, _AGENT_COLS)
        i_queue = _resolve_col(header, _QUEUE_COLS)
        i_corr = _resolve_col(header, _CORR_COLS)
        for row in rows:
            name = (_at(row, i_name) or "?").strip()
            start = _numf(_at(row, i_start))
            end = _numf(_at(row, i_end))
            dur = end - start if end > start else _numf(_at(row, i_dur))
            agent = _at(row, i_agent) or "0"
            queue = _at(row, i_queue) or "0"
            corr = _at(row, i_corr)

            _bump_agg(by_name, name, dur)
            by_key[(agent, queue)].append((start, end, name))
            if corr:
                dur_by_corr[corr].append(dur)
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return KernelBundle(
        by_name=by_name,
        by_key=dict(by_key),
        dur_by_corr=dict(dur_by_corr),
        window_start=window_start,
        window_end=window_end,
        count=count,
        source="trace",
    )


def _build_api_bundle_from_csv(files: list[Path]) -> ApiBundle:
    by_name: dict[str, dict] = {}
    direct_launches: list[tuple[str, float]] = []
    graph_launches: list[tuple[str, float]] = []
    sync_count = 0
    sync_total_ns = 0.0
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        rows = _iter_csv_reader(path)
        header = next(rows, None)
        if header is None:
            continue
        i_name = _resolve_col(header, _API_NAME_COLS)
        i_start = _resolve_col(header, _START_COLS)
        i_end = _resolve_col(header, _END_COLS)
        i_dur = _resolve_col(header, _DUR_COLS)
        i_corr = _resolve_col(header, _CORR_COLS)
        for row in rows:
            name = _at(row, i_name) or "?"
            start = _numf(_at(row, i_start))
            end = _numf(_at(row, i_end))
            dur = end - start if end > start else _numf(_at(row, i_dur))
            corr = _at(row, i_corr)

            _bump_agg(by_name, name, dur)
            if name.lower() in _SYNC_APIS:
                sync_count += 1
                sync_total_ns += dur
            if _is_graph_launch_api(name):
                graph_launches.append((corr, dur))
            elif _is_launch_api(name):
                direct_launches.append((corr, dur))
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return ApiBundle(
        by_name=by_name,
        direct_launches=direct_launches,
        graph_launches=graph_launches,
        sync_count=sync_count,
        sync_total_ns=sync_total_ns,
        window_start=window_start,
        window_end=window_end,
        count=count,
        source="trace",
    )


def _build_memcpy_bundle_from_csv(files: list[Path]) -> MemcpyBundle:
    by_dir: dict[str, dict] = {}
    bytes_available = False
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        rows = _iter_csv_reader(path)
        header = next(rows, None)
        if header is None:
            continue
        i_dir = _resolve_col(header, _MEMCPY_DIR_COLS)
        i_start = _resolve_col(header, _START_COLS)
        i_end = _resolve_col(header, _END_COLS)
        i_dur = _resolve_col(header, _DUR_COLS)
        i_bytes = _resolve_col(header, _MEMCPY_BYTES_COLS)
        if i_bytes >= 0:
            bytes_available = True
        for row in rows:
            direction = _normalize_direction(_at(row, i_dir) or "")
            start = _numf(_at(row, i_start))
            end = _numf(_at(row, i_end))
            dur = end - start if end > start else _numf(_at(row, i_dur))
            b = _numf(_at(row, i_bytes)) if i_bytes >= 0 else 0.0

            entry = by_dir.setdefault(direction, {"count": 0, "total_ns": 0.0, "bytes": 0.0})
            entry["count"] += 1
            entry["total_ns"] += dur
            entry["bytes"] += b
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return MemcpyBundle(
        by_dir=by_dir,
        bytes_available=bytes_available,
        window_start=window_start,
        window_end=window_end,
        count=count,
    )


# ---------------------------------------------------------------------------
# JSON -> bundle adapters (best-effort; see module docstring)
# ---------------------------------------------------------------------------


def _json_root(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    nested = data.get("rocprofiler-sdk-json-tool")
    return nested if isinstance(nested, dict) else data


def _json_records(root: dict, key: str) -> list[dict]:
    buffer_records = root.get("buffer_records")
    source = buffer_records if isinstance(buffer_records, dict) else root
    records = source.get(key) if isinstance(source, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _json_string_lookup(root: dict, section: str, id_key: str, name_keys: tuple[str, ...]) -> dict:
    strings = root.get("strings")
    entries = strings.get(section) if isinstance(strings, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = entry.get(id_key)
        for nk in name_keys:
            if entry.get(nk):
                out[eid] = str(entry[nk])
                break
    return out


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _json_agent_id(d: dict) -> str:
    dispatch_info = d.get("dispatch_info")
    if isinstance(dispatch_info, dict) and "agent_id" in dispatch_info:
        return str(dispatch_info["agent_id"])
    return str(d.get("agent_id", "0"))


def _build_kernel_bundle_from_json(files: list[Path]) -> KernelBundle:
    by_name: dict[str, dict] = {}
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    dur_by_corr: dict[str, list[float]] = defaultdict(list)
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        root = _json_root(_load_json(path))
        dispatches = _json_records(root, "kernel_dispatch")
        names = _json_string_lookup(
            root,
            "kernel_symbol_info",
            "kernel_id",
            ("formatted_kernel_name", "kernel_name", "name"),
        )
        for d in dispatches:
            kid = d.get("kernel_id")
            name = str(d.get("name") or names.get(kid) or f"kernel_{kid}").strip()
            start = _numf(d.get("start_timestamp"))
            end = _numf(d.get("end_timestamp"))
            dur = end - start if end > start else 0.0
            agent = _json_agent_id(d) or "0"
            queue = str(d.get("queue_id", "0"))
            corr = str(d.get("correlation_id", ""))

            _bump_agg(by_name, name, dur)
            by_key[(agent, queue)].append((start, end, name))
            if corr:
                dur_by_corr[corr].append(dur)
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return KernelBundle(
        by_name=by_name,
        by_key=dict(by_key),
        dur_by_corr=dict(dur_by_corr),
        window_start=window_start,
        window_end=window_end,
        count=count,
        source="json",
    )


def _build_api_bundle_from_json(files: list[Path]) -> ApiBundle:
    by_name: dict[str, dict] = {}
    direct_launches: list[tuple[str, float]] = []
    graph_launches: list[tuple[str, float]] = []
    sync_count = 0
    sync_total_ns = 0.0
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        root = _json_root(_load_json(path))
        for d in _json_records(root, "hip_api"):
            start = _numf(d.get("start_timestamp"))
            end = _numf(d.get("end_timestamp"))
            dur = end - start if end > start else 0.0
            name = str(d.get("name") or d.get("function") or "?")
            corr = str(d.get("correlation_id", ""))

            _bump_agg(by_name, name, dur)
            if name.lower() in _SYNC_APIS:
                sync_count += 1
                sync_total_ns += dur
            if _is_graph_launch_api(name):
                graph_launches.append((corr, dur))
            elif _is_launch_api(name):
                direct_launches.append((corr, dur))
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return ApiBundle(
        by_name=by_name,
        direct_launches=direct_launches,
        graph_launches=graph_launches,
        sync_count=sync_count,
        sync_total_ns=sync_total_ns,
        window_start=window_start,
        window_end=window_end,
        count=count,
        source="json",
    )


def _build_memcpy_bundle_from_json(files: list[Path]) -> MemcpyBundle:
    by_dir: dict[str, dict] = {}
    bytes_available = False
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    for path in files:
        root = _json_root(_load_json(path))
        for d in _json_records(root, "memory_copy"):
            start = _numf(d.get("start_timestamp"))
            end = _numf(d.get("end_timestamp"))
            dur = end - start if end > start else 0.0
            direction = _normalize_direction(str(d.get("direction") or d.get("copy_kind") or ""))
            b = d.get("bytes") if d.get("bytes") is not None else d.get("size")
            if b is not None:
                bytes_available = True

            entry = by_dir.setdefault(direction, {"count": 0, "total_ns": 0.0, "bytes": 0.0})
            entry["count"] += 1
            entry["total_ns"] += dur
            entry["bytes"] += _numf(b)
            window_start = min(window_start, start)
            window_end = max(window_end, end)
            count += 1
    if count == 0:
        window_start = window_end = 0.0
    return MemcpyBundle(
        by_dir=by_dir,
        bytes_available=bytes_available,
        window_start=window_start,
        window_end=window_end,
        count=count,
    )


def _kernel_bundle_from_stats(disc: DiscoveredReport) -> KernelBundle:
    stats_rows = [row for f in disc.kernel_stats for row in _stats_rows_from_csv(f)]
    if not stats_rows:
        return KernelBundle()
    return KernelBundle(by_name=_aggregate_stats(stats_rows), source="stats")


# ---------------------------------------------------------------------------
# Bundle cache — memoize one streaming pass per report per process
# ---------------------------------------------------------------------------
#
# ``summary`` runs every subcommand in one process against the same report;
# without this, each section would re-parse the same multi-hundred-MB CSV
# from scratch. Keyed by file identity (path + size + mtime), so a changed
# trace on disk is picked up rather than served stale.

_BUNDLE_CACHE: dict[tuple, object] = {}


def _files_key(files: list[Path]) -> tuple:
    key = []
    for f in files:
        try:
            st = f.stat()
            key.append((str(f), st.st_size, st.st_mtime_ns))
        except OSError:
            key.append((str(f), -1, -1))
    return tuple(sorted(key))


def _get_kernel_bundle(disc: DiscoveredReport) -> KernelBundle:
    if disc.kernel_trace:
        cache_key = ("kernel", _files_key(disc.kernel_trace))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_kernel_bundle_from_csv(disc.kernel_trace)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("kernel_json", _files_key(disc.json_files))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_kernel_bundle_from_json(disc.json_files)
        return _BUNDLE_CACHE[cache_key]
    return _kernel_bundle_from_stats(disc)


def _get_api_bundle(disc: DiscoveredReport) -> ApiBundle:
    if disc.hip_api_trace:
        cache_key = ("api", _files_key(disc.hip_api_trace))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_api_bundle_from_csv(disc.hip_api_trace)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("api_json", _files_key(disc.json_files))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_api_bundle_from_json(disc.json_files)
        return _BUNDLE_CACHE[cache_key]
    return ApiBundle()


def _get_memcpy_bundle(disc: DiscoveredReport) -> MemcpyBundle:
    if disc.memory_copy_trace:
        cache_key = ("memcpy", _files_key(disc.memory_copy_trace))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_memcpy_bundle_from_csv(disc.memory_copy_trace)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("memcpy_json", _files_key(disc.json_files))
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_memcpy_bundle_from_json(disc.json_files)
        return _BUNDLE_CACHE[cache_key]
    return MemcpyBundle()


def _load_kernels(disc: DiscoveredReport) -> tuple[dict[str, dict], str]:
    """Return (per-kernel aggregate, source label): 'trace', 'json', 'stats', or ''."""
    bundle = _get_kernel_bundle(disc)
    return bundle.by_name, bundle.source


# ---------------------------------------------------------------------------
# Subcommand: files  # noqa: ERA001
# ---------------------------------------------------------------------------


def _print_file_buckets(disc: DiscoveredReport) -> bool:
    """Print the discovered-file-counts block; return whether anything was found."""
    buckets = (
        ("kernel_trace", disc.kernel_trace),
        ("kernel_stats", disc.kernel_stats),
        ("hip_api_trace", disc.hip_api_trace),
        ("hip_api_stats", disc.hip_api_stats),
        ("memory_copy_trace", disc.memory_copy_trace),
        ("memory_copy_stats", disc.memory_copy_stats),
        ("domain_stats", disc.domain_stats),
        ("agent_info", disc.agent_info),
    )
    any_found = False
    print("\nDiscovered CSV files:")  # noqa: T201
    for label, files in buckets:
        if not files:
            continue
        any_found = True
        rows = sum(_csv_row_count(f) for f in files)
        print(f"  {label:<20s}: {len(files)} file(s), {rows} row(s)")  # noqa: T201
    for label, files in (("json", disc.json_files), ("rocpd sqlite (.db)", disc.db_files)):
        if files:
            any_found = True
            print(f"  {label:<20s}: {len(files)} file(s)")  # noqa: T201
    if disc.other_csv:
        print(f"  {'other .csv':<20s}: {len(disc.other_csv)} file(s) (unrecognized — not analyzed)")  # noqa: T201
    if not any_found:
        print("  (none found)")  # noqa: T201
    return any_found


def cmd_files(ns: argparse.Namespace) -> None:
    """Discover output files, process captures, agents, and row counts."""
    disc = discover(ns.report)
    print(f"Report root: {disc.root}")  # noqa: T201

    if not _print_file_buckets(disc):
        print(f"\n(no rocprofv3 output recognized under {disc.root})")  # noqa: T201
        return

    process_dirs = _process_dirs(disc)
    if process_dirs:
        print("\nProcess captures:")  # noqa: T201
        for d in process_dirs:
            print(f"  {_describe_process_dir(d, disc.root)}")  # noqa: T201

    agents = _load_agents(disc)
    if agents:
        print("\nGPU/CPU agents:")  # noqa: T201
        for a in agents:
            print(f"  agent {a['agent']:<6s} {a['name']:<24s} gfx={a['gfx']:<12s} type={a['type']}")  # noqa: T201

    print("\nStart with `kernels`, `families`, or `summary`.")  # noqa: T201


# ---------------------------------------------------------------------------
# Subcommand: kernels  # noqa: ERA001
# ---------------------------------------------------------------------------


def cmd_kernels(ns: argparse.Namespace) -> None:
    """Top GPU kernels by total execution time, with a library-family column."""
    disc = discover(ns.report)
    agg, source = _load_kernels(disc)
    if not agg:
        print(  # noqa: T201
            "(no kernel data found — no *_kernel_trace.csv or *_kernel_stats.csv under report path)"
        )
        return

    total_ns = sum(e["total_ns"] for e in agg.values())
    total_calls = sum(e["calls"] for e in agg.values())
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["total_ns"])[: ns.top]

    print(f"Kernel data source: {source}")  # noqa: T201
    header = f"{'Kernel':<44s} {'Family':<28s} {'Calls':>7s} {'Total':>9s} {'Avg':>9s} {'Min':>9s} {'Max':>9s} {'%GPU':>6s}"
    print(header)  # noqa: T201
    print("-" * len(header))  # noqa: T201
    for name, e in ranked:
        fam = _classify_family(name)
        avg = e["total_ns"] / e["calls"] if e["calls"] else 0.0
        pct = e["total_ns"] / total_ns * 100 if total_ns else 0.0
        min_ns = e["min_ns"] if e["min_ns"] != float("inf") else 0.0
        print(  # noqa: T201
            f"{_truncate(_short_name(name), 44):<44s} {fam:<28s} {e['calls']:>7d} {_fmt_ns(e['total_ns']):>9s} "
            f"{_fmt_ns(avg):>9s} {_fmt_ns(min_ns):>9s} {_fmt_ns(e['max_ns']):>9s} {pct:>5.1f}%"
        )
    print(f"\nTotal GPU kernel time (all kernels): {_fmt_ns(total_ns)}")  # noqa: T201
    print(f"Total kernel launches: {total_calls}")  # noqa: T201


# ---------------------------------------------------------------------------
# Subcommand: families  # noqa: ERA001
# ---------------------------------------------------------------------------


def cmd_families(ns: argparse.Namespace) -> None:
    """GPU time grouped by kernel-library family, flagging fallback-family hot spots."""
    disc = discover(ns.report)
    agg, source = _load_kernels(disc)
    if not agg:
        print(  # noqa: T201
            "(no kernel data found — no *_kernel_trace.csv or *_kernel_stats.csv under report path)"
        )
        return

    fam_totals: dict[str, dict] = {}
    for name, e in agg.items():
        fam = _classify_family(name)
        entry = fam_totals.setdefault(
            fam, {"calls": 0, "total_ns": 0.0, "top_name": "", "top_ns": 0.0}
        )
        entry["calls"] += e["calls"]
        entry["total_ns"] += e["total_ns"]
        if e["total_ns"] > entry["top_ns"]:
            entry["top_ns"] = e["total_ns"]
            entry["top_name"] = name

    total_ns = sum(e["total_ns"] for e in fam_totals.values())
    ordered = sorted(fam_totals.items(), key=lambda kv: -kv[1]["total_ns"])

    print(f"Kernel data source: {source}")  # noqa: T201
    header = (
        f"{'Family':<32s} {'Calls':>8s} {'Total':>10s} {'%GPU':>6s}  {'Top kernel in family':<40s}"
    )
    print(header)  # noqa: T201
    print("-" * len(header))  # noqa: T201
    for fam, e in ordered:
        pct = e["total_ns"] / total_ns * 100 if total_ns else 0.0
        print(  # noqa: T201
            f"{fam:<32s} {e['calls']:>8d} {_fmt_ns(e['total_ns']):>10s} {pct:>5.1f}%  "
            f"{_truncate(_short_name(e['top_name']), 40):<40s}"
        )

    flags = [
        (fam, e["top_name"], e["total_ns"] / total_ns * 100 if total_ns else 0.0)
        for fam, e in ordered
        if fam in FALLBACK_FAMILIES
        and e["total_ns"] > 0
        and (e["total_ns"] / total_ns * 100 if total_ns else 0.0)
        >= _FALLBACK_FAMILY_SHARE_THRESHOLD
        and _GEMM_ATTN_RE.search(e["top_name"])
    ]
    if flags:
        print(  # noqa: T201
            "\n*** Finding: GEMM/attention-shaped work is landing in a fallback family "
            "instead of AITER/CK/hipBLASLt ***"
        )
        for fam, top_name, share in flags:
            print(  # noqa: T201
                f"  {fam}: '{_short_name(top_name)}' — {share:.1f}% of GPU time. Check "
                f"dispatch/tuning config (e.g. AITER_LOG_TUNED_CONFIG=1) instead of accepting "
                f"the fallback kernel."
            )


# ---------------------------------------------------------------------------
# Subcommand: idle_gaps  # noqa: ERA001
# ---------------------------------------------------------------------------

_IDLE_GAP_THRESHOLD_NS = 1000.0


def _gaps_for_key(
    key: tuple[str, str], evs: list[tuple[float, float, str]]
) -> tuple[float, list[tuple[tuple[str, str], str, str, float]]]:
    """Merge one agent/queue's kernel intervals; return (busy_ns, gaps > threshold)."""
    evs = sorted(evs)
    merged = _merge_intervals([(s, e) for s, e, _n in evs])
    busy_ns = sum(e - s for s, e in merged)

    end_name = {e: n for _s, e, n in evs}
    start_name = {s: n for s, _e, n in evs}
    gaps: list[tuple[tuple[str, str], str, str, float]] = []
    for i in range(1, len(merged)):
        gap = merged[i][0] - merged[i - 1][1]
        if gap > _IDLE_GAP_THRESHOLD_NS:
            prev_name = end_name.get(merged[i - 1][1], "?")
            next_name = start_name.get(merged[i][0], "?")
            gaps.append((key, prev_name, next_name, gap))
    return busy_ns, gaps


def cmd_idle_gaps(ns: argparse.Namespace) -> None:
    """GPU busy vs. idle over the trace window, per agent/queue, largest gaps."""
    disc = discover(ns.report)
    bundle = _get_kernel_bundle(disc)
    if bundle.count < 2:  # noqa: PLR2004
        if disc.kernel_stats and not disc.kernel_trace:
            print(  # noqa: T201
                "(idle-gap analysis needs per-event timestamps from *_kernel_trace.csv; "
                "only aggregate *_kernel_stats.csv was found.)"
            )
        else:
            print("(fewer than 2 kernel events with timestamps found.)")  # noqa: T201
        return

    gaps: list[tuple[tuple[str, str], str, str, float]] = []
    total_busy = 0.0
    for key, evs in bundle.by_key.items():
        busy_ns, key_gaps = _gaps_for_key(key, evs)
        total_busy += busy_ns
        gaps.extend(key_gaps)

    gaps.sort(key=lambda x: -x[3])
    total_idle = sum(g[3] for g in gaps)
    window_ns = bundle.window_end - bundle.window_start

    print(f"Capture window: {_fmt_ns(window_ns)}")  # noqa: T201
    print(f"GPU busy (union per agent/queue): {_fmt_ns(total_busy)}")  # noqa: T201
    denom = total_busy + total_idle
    pct_idle = total_idle / denom * 100 if denom else 0.0
    print(f"GPU idle (intra-key gaps > 1us): {_fmt_ns(total_idle)} ({pct_idle:.1f}%)")  # noqa: T201
    print(f"Idle gaps found: {len(gaps)}")  # noqa: T201

    top = gaps[: ns.top]
    if top:
        print(f"\nTop {len(top)} gaps:")  # noqa: T201
        print(f"  {'Agent/Queue':<16s} {'Gap':>10s}  {'After':<32s} -> {'Before':<32s}")  # noqa: T201
        print("  " + "-" * 92)  # noqa: T201
        for (agent, queue), prev_name, next_name, gap in top:
            key_str = f"{agent}/{queue}"
            print(  # noqa: T201
                f"  {key_str:<16s} {_fmt_ns(gap):>10s}  {_truncate(_short_name(prev_name), 32):<32s} -> "
                f"{_truncate(_short_name(next_name), 32):<32s}"
            )


# ---------------------------------------------------------------------------
# Subcommand: cpu_overhead  # noqa: ERA001
# ---------------------------------------------------------------------------

_LAUNCH_API_PREFIXES = (
    "hiplaunchkernel",
    "hipmodulelaunchkernel",
    "hipextlaunchkernel",
    "hipextmodulelaunchkernel",
    "hipgraphlaunch",
)
_SYNC_APIS = frozenset({"hipstreamsynchronize", "hipdevicesynchronize", "hipeventsynchronize"})
_TOP_API_ROWS = 10
_LAUNCH_BOUND_RATIO_THRESHOLD = 1.0


def _is_launch_api(name: str) -> bool:
    lname = name.lower()
    return any(lname.startswith(p) for p in _LAUNCH_API_PREFIXES)


def _is_graph_launch_api(name: str) -> bool:
    return name.lower().startswith("hipgraphlaunch")


def _print_api_table(ranked: list[tuple[str, dict]]) -> None:
    print(f"\n{'API':<36s} {'Calls':>8s} {'Total':>10s} {'Avg':>10s}")  # noqa: T201
    print("-" * 68)  # noqa: T201
    for name, e in ranked:
        avg = e["total_ns"] / e["calls"] if e["calls"] else 0.0
        print(  # noqa: T201
            f"{_truncate(name, 36):<36s} {e['calls']:>8d} {_fmt_ns(e['total_ns']):>10s} {_fmt_ns(avg):>10s}"
        )


def _launch_bound_from_correlation(
    launch_calls: list[tuple[str, float]], dur_by_corr: dict[str, list[float]]
) -> None:
    matched_cpu = []
    matched_gpu = []
    for corr, dur_ns in launch_calls:
        durs = dur_by_corr.get(corr) if corr else None
        if durs:
            matched_cpu.append(dur_ns)
            matched_gpu.append(sum(durs) / len(durs))

    if not matched_cpu:
        print(  # noqa: T201
            "\n(no matching Correlation_Id between HIP API and kernel trace — "
            "cannot compute a matched launch-bound ratio)"
        )
        return

    avg_cpu = sum(matched_cpu) / len(matched_cpu)
    avg_gpu = sum(matched_gpu) / len(matched_gpu)
    print(f"\nKernel launch overhead ({len(matched_cpu)} matched by Correlation_Id):")  # noqa: T201
    print(f"  Avg CPU launch: {_fmt_ns(avg_cpu)}")  # noqa: T201
    print(f"  Avg GPU exec:   {_fmt_ns(avg_gpu)}")  # noqa: T201
    if avg_gpu > 0:
        ratio = avg_cpu / avg_gpu
        print(f"  CPU/GPU ratio:  {ratio:.2f}x")  # noqa: T201
        if ratio > _LAUNCH_BOUND_RATIO_THRESHOLD:
            print("  *** LAUNCH-BOUND — CPU launch overhead exceeds GPU execution time ***")  # noqa: T201


def _cpu_overhead_from_trace(api_bundle: ApiBundle, kernel_bundle: KernelBundle) -> None:
    total_calls = api_bundle.count
    total_ns = sum(e["total_ns"] for e in api_bundle.by_name.values())
    print(f"Total HIP API calls: {total_calls}")  # noqa: T201
    print(f"Total CPU time in HIP APIs: {_fmt_ns(total_ns)}")  # noqa: T201

    _print_api_table(
        sorted(api_bundle.by_name.items(), key=lambda kv: -kv[1]["total_ns"])[:_TOP_API_ROWS]
    )

    print(  # noqa: T201
        f"\nSynchronization stalls: {api_bundle.sync_count} calls, {_fmt_ns(api_bundle.sync_total_ns)}"
    )

    launch_calls = api_bundle.direct_launches + api_bundle.graph_launches
    window_ns = api_bundle.window_end - api_bundle.window_start
    rate = len(launch_calls) / (window_ns / _NS_PER_SEC) if window_ns > 0 else 0.0
    print(  # noqa: T201
        f"\nKernel launch calls: {len(launch_calls)}  ({rate:.1f} launches/sec over the capture window)"
    )

    if kernel_bundle.dur_by_corr and launch_calls:
        _launch_bound_from_correlation(launch_calls, kernel_bundle.dur_by_corr)
    elif launch_calls:
        print(  # noqa: T201
            "\n(no *_kernel_trace.csv found — cannot compute a matched CPU-launch-vs-GPU-exec ratio)"
        )


def _cpu_overhead_from_stats(stats: dict[str, dict], disc: DiscoveredReport) -> None:
    total_calls = sum(s["calls"] for s in stats.values())
    total_ns = sum(s["total_ns"] for s in stats.values())
    print(f"Total HIP API calls: {total_calls}  (aggregated from *_hip_api_stats.csv)")  # noqa: T201
    print(f"Total CPU time in HIP APIs: {_fmt_ns(total_ns)}")  # noqa: T201
    _print_api_table(sorted(stats.items(), key=lambda kv: -kv[1]["total_ns"])[:_TOP_API_ROWS])

    launch_ns = sum(s["total_ns"] for n, s in stats.items() if _is_launch_api(n))
    launch_calls = sum(s["calls"] for n, s in stats.items() if _is_launch_api(n))
    kernel_agg, _src = _load_kernels(disc)
    kernel_ns = sum(e["total_ns"] for e in kernel_agg.values())
    print(f"\nKernel launch calls: {launch_calls}, total CPU time {_fmt_ns(launch_ns)}")  # noqa: T201
    if kernel_ns > 0 and launch_ns > 0:
        ratio = launch_ns / kernel_ns
        print(  # noqa: T201
            f"Coarse CPU-launch-time / GPU-kernel-time ratio: {ratio:.2f}x (aggregate, not per-launch matched)"
        )
        if ratio > _LAUNCH_BOUND_RATIO_THRESHOLD:
            print("*** Possibly launch-bound — coarse launch time exceeds kernel GPU time ***")  # noqa: T201


def cmd_cpu_overhead(ns: argparse.Namespace) -> None:
    """HIP API time, launch rate, sync stalls, and a launch-bound heuristic."""
    disc = discover(ns.report)
    api_bundle = _get_api_bundle(disc)
    if api_bundle.count:
        _cpu_overhead_from_trace(api_bundle, _get_kernel_bundle(disc))
        return
    stats = _load_api_stats(disc)
    if not stats:
        print(  # noqa: T201
            "(no HIP API data found — no *_hip_api_trace.csv or *_hip_api_stats.csv under report path)"
        )
        return
    _cpu_overhead_from_stats(stats, disc)


# ---------------------------------------------------------------------------
# Subcommand: memory  # noqa: ERA001
# ---------------------------------------------------------------------------


def cmd_memory(ns: argparse.Namespace) -> None:
    """Memory copies by direction, bytes, time, and bandwidth."""
    disc = discover(ns.report)
    bundle = _get_memcpy_bundle(disc)
    if not bundle.count:
        print("(no memory copy data found — no *_memory_copy_trace.csv under report path)")  # noqa: T201
        return

    ordered = sorted(bundle.by_dir.items(), key=lambda kv: -kv[1]["total_ns"])
    if bundle.bytes_available:
        print(f"{'Direction':<10s} {'Count':>8s} {'Total':>10s} {'Bytes':>16s} {'Bandwidth':>12s}")  # noqa: T201
        print("-" * 60)  # noqa: T201
        for d, e in ordered:
            bw_gbps = (
                (e["bytes"] / _BYTES_PER_GB) / (e["total_ns"] / _NS_PER_SEC)
                if e["total_ns"] > 0
                else 0.0
            )
            print(  # noqa: T201
                f"{d:<10s} {e['count']:>8d} {_fmt_ns(e['total_ns']):>10s} {e['bytes']:>16,.0f} {bw_gbps:>10.1f}GB/s"
            )
        total_bytes = sum(e["bytes"] for e in bundle.by_dir.values())
        total_ns = sum(e["total_ns"] for e in bundle.by_dir.values())
        print(f"\nTotal memory copy: {total_bytes / _BYTES_PER_GB:.2f} GB in {_fmt_ns(total_ns)}")  # noqa: T201
    else:
        print(f"{'Direction':<10s} {'Count':>8s} {'Total':>10s}")  # noqa: T201
        print("-" * 34)  # noqa: T201
        for d, e in ordered:
            print(f"{d:<10s} {e['count']:>8d} {_fmt_ns(e['total_ns']):>10s}")  # noqa: T201
        total_ns = sum(e["total_ns"] for e in bundle.by_dir.values())
        print(f"\nTotal memory copy time: {_fmt_ns(total_ns)}")  # noqa: T201
        print(  # noqa: T201
            "(byte counts not available: this rocprofv3 capture's "
            "*_memory_copy_trace.csv has no Bytes/Size column — showing count and time only)"
        )


# ---------------------------------------------------------------------------
# Subcommand: graphs  # noqa: ERA001
# ---------------------------------------------------------------------------

_GRAPH_DEGRADED_KERNELS_PER_LAUNCH = 3.0
_GRAPH_DEGRADED_DIRECT_LAUNCH_FRACTION = 0.5


def _report_graph_counts(graph_calls: int, direct_calls: int, total_kernels: int) -> None:
    print(f"hipGraphLaunch calls: {graph_calls}")  # noqa: T201
    print(f"Direct launch calls (hipLaunchKernel/hipModuleLaunchKernel/...): {direct_calls}")  # noqa: T201
    print(f"Total kernels recorded: {total_kernels}")  # noqa: T201
    if graph_calls == 0:
        print(  # noqa: T201
            "\n(No HIP graph launches detected — per-kernel attribution via direct "
            "launch APIs should be reliable.)"
        )
        return

    kernels_per_launch = total_kernels / graph_calls
    print(f"\nKernels per graph launch (coarse): {kernels_per_launch:.1f}")  # noqa: T201
    degraded = (
        direct_calls < total_kernels * _GRAPH_DEGRADED_DIRECT_LAUNCH_FRACTION
        or kernels_per_launch > _GRAPH_DEGRADED_KERNELS_PER_LAUNCH
    )
    if degraded:
        print(  # noqa: T201
            "\n*** Kernel attribution is likely DEGRADED: most kernels execute under "
            "hipGraphLaunch rather than individual launch calls, so per-kernel timing "
            "and family breakdowns above may be missing or misattributed. Recommendation: "
            "re-capture with HIP graph capture disabled (eager mode) for reliable "
            "per-kernel attribution. ***"
        )


def _report_matched_graph_kernels(
    graph_launches: list[tuple[str, float]], dur_by_corr: dict[str, list[float]]
) -> None:
    matched = [dur_by_corr[corr] for corr, _dur in graph_launches if corr in dur_by_corr]
    if not matched:
        return
    counts = [len(ks) for ks in matched]
    times = [sum(ks) for ks in matched]
    print(f"\nGraph launches with matched kernels (by Correlation_Id): {len(matched)}")  # noqa: T201
    print(  # noqa: T201
        f"  Avg kernels per launch: {sum(counts) / len(counts):.1f}  (min {min(counts)}, max {max(counts)})"
    )
    print(f"  Avg GPU time per launch: {_fmt_ns(sum(times) / len(times))}")  # noqa: T201


def cmd_graphs(ns: argparse.Namespace) -> None:
    """HIP graph launches, and detection of graph-degraded kernel attribution."""
    disc = discover(ns.report)
    kernel_bundle = _get_kernel_bundle(disc)
    total_kernels = kernel_bundle.count or sum(e["calls"] for e in kernel_bundle.by_name.values())

    api_bundle = _get_api_bundle(disc)
    if api_bundle.count:
        _report_graph_counts(
            len(api_bundle.graph_launches), len(api_bundle.direct_launches), total_kernels
        )
        if api_bundle.graph_launches and kernel_bundle.dur_by_corr:
            _report_matched_graph_kernels(api_bundle.graph_launches, kernel_bundle.dur_by_corr)
        return

    stats = _load_api_stats(disc)
    if not stats:
        print("(no HIP API data found — cannot detect HIP graph launches)")  # noqa: T201
        return
    graph_calls = sum(s["calls"] for n, s in stats.items() if _is_graph_launch_api(n))
    direct_calls = sum(
        s["calls"] for n, s in stats.items() if _is_launch_api(n) and not _is_graph_launch_api(n)
    )
    _report_graph_counts(graph_calls, direct_calls, total_kernels)


# ---------------------------------------------------------------------------
# Subcommand: host_idle  # noqa: ERA001
# ---------------------------------------------------------------------------

_HOST_IDLE_BUSY_PCT_THRESHOLD = 5.0


def _kernel_union_ns(bundle: KernelBundle) -> float:
    """Global busy time across all agent/queue keys (no double-counting overlaps)."""
    if not bundle.by_key:
        return 0.0
    all_intervals = [(s, e) for evs in bundle.by_key.values() for s, e, _n in evs]
    return sum(e - s for s, e in _merge_intervals(all_intervals))


def cmd_host_idle(ns: argparse.Namespace) -> None:
    """Detect a mostly host-idle capture (missed load, or an idle server)."""
    disc = discover(ns.report)
    kernel_bundle = _get_kernel_bundle(disc)
    api_bundle = _get_api_bundle(disc)
    mem_bundle = _get_memcpy_bundle(disc)

    bundles = (kernel_bundle, api_bundle, mem_bundle)
    total_events = sum(b.count for b in bundles)
    if not total_events:
        if kernel_bundle.by_name:
            print(  # noqa: T201
                "(only aggregate stats found — no per-event timestamps to compute a capture "
                "window; cannot run the host-idle check. Use `kernels`/`families` instead.)"
            )
        else:
            print("(no timestamped data found at all — nothing to check.)")  # noqa: T201
        return

    starts = [b.window_start for b in bundles if b.count]
    ends = [b.window_end for b in bundles if b.count]
    window_ns = max(ends) - min(starts) if starts else 0.0
    busy_ns = _kernel_union_ns(kernel_bundle)
    busy_pct = busy_ns / window_ns * 100 if window_ns > 0 else 0.0

    print(f"Capture window: {_fmt_ns(window_ns)}")  # noqa: T201
    print(f"GPU kernel activity: {_fmt_ns(busy_ns)} ({busy_pct:.2f}% of window)")  # noqa: T201
    print(f"Kernels recorded: {kernel_bundle.count}")  # noqa: T201

    if not kernel_bundle.count or busy_pct < _HOST_IDLE_BUSY_PCT_THRESHOLD:
        print(  # noqa: T201
            "\n*** VERDICT: trace looks mostly HOST-IDLE — the GPU did little or no work "
            "during the capture window. The capture likely missed the load (started before "
            "or after the workload ran) or the server was idle. Recommendation: re-capture "
            "under active load, or widen the capture window to cover the workload's steady "
            "state. ***"
        )
    else:
        print(  # noqa: T201
            f"\nVerdict: GPU active for {busy_pct:.1f}% of the capture window — trace looks valid."
        )


# ---------------------------------------------------------------------------
# Subcommand: query  # noqa: ERA001
# ---------------------------------------------------------------------------


def cmd_query(ns: argparse.Namespace) -> None:
    """Run arbitrary SQL against a rocpd SQLite export, if one is present."""
    disc = discover(ns.report)
    if not disc.db_files:
        print(  # noqa: T201
            "(no rocpd SQLite (.db) file found under report path. `query` only works "
            "against ROCm 7's rocpd SQLite output; use `kernels`/`families`/`summary`/etc. "
            "for CSV or JSON traces.)"
        )
        return
    conn = sqlite3.connect(str(disc.db_files[0]))
    try:
        cur = conn.execute(ns.sql)
        if cur.description:
            headers = [d[0] for d in cur.description]
            print("\t".join(headers))  # noqa: T201
            for row in cur.fetchall():
                print("\t".join(str(v) for v in row))  # noqa: T201
        else:
            print("(no results)")  # noqa: T201
    except sqlite3.OperationalError as exc:
        print(f"SQL error: {exc}", file=sys.stderr)  # noqa: T201
        sys.exit(1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subcommand: summary  # noqa: ERA001
# ---------------------------------------------------------------------------

_SUMMARY_LINE_CAP = 40


def _capped(fn, ns: argparse.Namespace) -> str:  # noqa: ANN001
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    lines = buf.getvalue().splitlines()
    if len(lines) > _SUMMARY_LINE_CAP:
        lines = [
            *lines[:_SUMMARY_LINE_CAP],
            f"... ({len(lines) - _SUMMARY_LINE_CAP} more lines omitted)",
        ]
    return "\n".join(lines)


def cmd_summary(ns: argparse.Namespace) -> None:
    """All-in-one analysis: files, validity checks, kernels/families/idle/overhead/memory/graphs."""
    ns.top = getattr(ns, "top", 15)

    print("=" * 78)  # noqa: T201
    print("  ROCPROFV3 TRACE SUMMARY")  # noqa: T201
    print("=" * 78)  # noqa: T201

    sections = (
        ("Files", cmd_files),
        ("Trace Validity (host-idle check)", cmd_host_idle),
        ("Top Kernels", cmd_kernels),
        ("Kernel Library Families", cmd_families),
        ("GPU Idle Gaps", cmd_idle_gaps),
        ("CPU / HIP API Overhead", cmd_cpu_overhead),
        ("Memory Copies", cmd_memory),
        ("HIP Graph Launches", cmd_graphs),
    )
    for title, fn in sections:
        print(f"\n## {title}\n")  # noqa: T201
        print(_capped(fn, ns))  # noqa: T201


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_report_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "report", help="rocprofv3 output directory, or a specific trace/stats/json/db file"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI: one subparser per ``cmd_*`` function."""
    parser = argparse.ArgumentParser(
        description="rocprofv3 trace analysis toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("files", help="Discover output files, processes, agents, row counts")
    _add_report_arg(p)

    p = sub.add_parser("kernels", help="Top GPU kernels by total time")
    _add_report_arg(p)
    p.add_argument("--top", type=int, default=15)

    p = sub.add_parser("families", help="GPU time grouped by kernel library family")
    _add_report_arg(p)

    p = sub.add_parser("idle_gaps", help="GPU busy vs idle, largest gaps")
    _add_report_arg(p)
    p.add_argument("--top", type=int, default=10)

    p = sub.add_parser("cpu_overhead", help="HIP API launch overhead and launch-bound heuristic")
    _add_report_arg(p)

    p = sub.add_parser("memory", help="Memory copies by direction, bytes, bandwidth")
    _add_report_arg(p)

    p = sub.add_parser("graphs", help="HIP graph launches and attribution-degradation check")
    _add_report_arg(p)

    p = sub.add_parser("host_idle", help="Detect a mostly host-idle / load-missed capture")
    _add_report_arg(p)

    p = sub.add_parser("query", help="Run arbitrary SQL against a rocpd SQLite export, if present")
    _add_report_arg(p)
    p.add_argument("sql")

    p = sub.add_parser("summary", help="All-in-one analysis")
    _add_report_arg(p)
    p.add_argument("--top", type=int, default=15)

    return parser


_COMMANDS = {
    "files": cmd_files,
    "kernels": cmd_kernels,
    "families": cmd_families,
    "idle_gaps": cmd_idle_gaps,
    "cpu_overhead": cmd_cpu_overhead,
    "memory": cmd_memory,
    "graphs": cmd_graphs,
    "host_idle": cmd_host_idle,
    "query": cmd_query,
    "summary": cmd_summary,
}


def main() -> None:
    """Entry point: parse argv and dispatch to the matching ``cmd_*`` function."""
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
