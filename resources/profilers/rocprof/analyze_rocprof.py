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
    python analyze_rocprof.py kernels <report> [--top N] [--window WINDOW]
    python analyze_rocprof.py families <report> [--window WINDOW]
    python analyze_rocprof.py idle_gaps <report> [--top N] [--window WINDOW]
    python analyze_rocprof.py cpu_overhead <report> [--window WINDOW]
    python analyze_rocprof.py memory <report> [--window WINDOW]
    python analyze_rocprof.py graphs <report> [--window WINDOW]
    python analyze_rocprof.py host_idle <report> [--window WINDOW]
    python analyze_rocprof.py query <report> "<sql>"
    python analyze_rocprof.py summary <report> [--top N] [--window WINDOW]

``--window`` (default ``load``, when the capture recorded a load phase):
selects which events a timeline analysis considers. A server capture
includes startup (weight load, warmup, KV init, ...) ahead of the actual
benchmarked load, which can dominate the trace and pollute the
kernel/family tables; ``load`` restricts analysis to the recorded load
phase (see ``capture_runtime``'s manifest ``load_window``/``capture_start``/
``capture_end`` fields). Other values: ``all`` (no filtering, the whole
run), ``startup`` (process launch through load start), or an explicit
``start_s:end_s`` pair of seconds relative to the trace's own earliest
timestamp. Falls back to ``all`` -- with a note in the output header --
whenever no load window was recorded or the trace's own timestamps don't
plausibly align with the manifest's recorded capture span, rather than
silently mis-slicing. ``query`` is not windowed (rocpd SQL runs against the
whole export).
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
from typing import TYPE_CHECKING, Any

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
        "serving-engine custom ops",
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

# A family whose per-call average dwarfs every other family's is evidence of
# either severe kernel-selection/occupancy trouble or a capture artifact
# specific to that kernel's launch shape, not something to cite as a literal
# %GPU share without checking further -- see _outlier_family_note.
_OUTLIER_AVG_MULTIPLE = 20.0


def _looks_like_triton_kernel(name: str) -> bool:
    """Structural fallback for a Triton JIT kernel with no ``triton_*`` prefix.

    Only kernels rocprofv3 renamed via torch.inductor carry a ``triton_``
    prefix (the ``_FAMILY_RULES`` entry above). A kernel compiled directly
    from a ``@triton.jit`` function (a serving engine's own custom ops:
    paged-attention helpers, MoE routing, and linear-attention/GDN kernels such as
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


def _boundary_pattern(sub: str) -> str:
    """Build a regex fragment matching ``sub`` only at an identifier boundary.

    Plain substring matching lets a marker land inside an unrelated
    identifier: ``topk_kernel`` (a ``PyTorch native`` marker) is a substring
    of the Triton-JIT kernel name ``atopk_kernel_kernel``, so naive ``in``
    matching misattributes it. Require that the character immediately before
    the match not be a lowercase letter or digit. Start-of-string, ``_``
    (the snake_case token separator), and structural characters such as
    ``::``, ``<``, `` ``, ``(``, ``,`` are all valid boundaries and still
    match; only a marker glued directly onto preceding letters/digits (no
    boundary) is rejected.
    """
    return rf"(?<![a-z0-9]){re.escape(sub)}"


_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (family, re.compile("|".join(_boundary_pattern(sub) for sub in subs)))
    for family, subs in _FAMILY_RULES
)


def _classify_family(name: str) -> str:
    """Classify a kernel name into a library family (best-effort, name-only)."""
    lname = name.lower()
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(lname):
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
        raise FileNotFoundError(f"report path not found: {report}")  # noqa: TRY003  # LW-920008; this is a boundary error that deliberately embeds the offending value for the operator to act on
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
    """One streaming pass over ``*_kernel_trace.csv`` (or a JSON/stats fallback).

    ``count``/``window_start``/``window_end`` describe the events actually
    kept (inside the requested ``window``, see ``resolve_window``);
    ``count_total`` is every event seen in the streaming pass regardless of
    ``window``, kept so callers can report "N of M dispatches" without a
    second pass.
    """

    by_name: dict[str, dict] = field(default_factory=dict)
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = field(default_factory=dict)
    dur_by_corr: dict[str, list[float]] = field(default_factory=dict)
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0
    count_total: int = 0
    source: str = ""


@dataclass
class ApiBundle:
    """One streaming pass over ``*_hip_api_trace.csv`` (or a JSON fallback). See ``KernelBundle``."""

    by_name: dict[str, dict] = field(default_factory=dict)
    direct_launches: list[tuple[str, float]] = field(default_factory=list)
    graph_launches: list[tuple[str, float]] = field(default_factory=list)
    sync_count: int = 0
    sync_total_ns: float = 0.0
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0
    count_total: int = 0
    source: str = ""


@dataclass
class MemcpyBundle:
    """One streaming pass over ``*_memory_copy_trace.csv`` (or a JSON fallback). See ``KernelBundle``."""

    by_dir: dict[str, dict] = field(default_factory=dict)
    bytes_available: bool = False
    window_start: float = 0.0
    window_end: float = 0.0
    count: int = 0
    count_total: int = 0


def _iter_csv_reader(path: Path) -> Iterator[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return
        yield header
        yield from reader


def _in_window(start_ns: float, window: tuple[float, float] | None) -> bool:
    """Whether an event's start timestamp falls inside *window* (inclusive both ends).

    ``window is None`` means "no filtering" (the 'all' window): every event
    is kept. Filtering keys off the event's *start*, per the load-window
    contract: an event that starts inside the window is attributed to it
    even if it runs slightly past ``window[1]``.
    """
    return window is None or (window[0] <= start_ns <= window[1])


def _build_kernel_bundle_from_csv(
    files: list[Path], window: tuple[float, float] | None = None
) -> KernelBundle:
    by_name: dict[str, dict] = {}
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    dur_by_corr: dict[str, list[float]] = defaultdict(list)
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
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
            count_total += 1
            start = _numf(_at(row, i_start))
            if not _in_window(start, window):
                continue
            name = (_at(row, i_name) or "?").strip()
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
        count_total=count_total,
        source="trace",
    )


def _build_api_bundle_from_csv(
    files: list[Path], window: tuple[float, float] | None = None
) -> ApiBundle:
    by_name: dict[str, dict] = {}
    direct_launches: list[tuple[str, float]] = []
    graph_launches: list[tuple[str, float]] = []
    sync_count = 0
    sync_total_ns = 0.0
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
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
            count_total += 1
            start = _numf(_at(row, i_start))
            if not _in_window(start, window):
                continue
            name = _at(row, i_name) or "?"
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
        count_total=count_total,
        source="trace",
    )


def _build_memcpy_bundle_from_csv(
    files: list[Path], window: tuple[float, float] | None = None
) -> MemcpyBundle:
    by_dir: dict[str, dict] = {}
    bytes_available = False
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
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
            count_total += 1
            start = _numf(_at(row, i_start))
            if not _in_window(start, window):
                continue
            direction = _normalize_direction(_at(row, i_dir) or "")
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
        count_total=count_total,
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


def _build_kernel_bundle_from_json(
    files: list[Path], window: tuple[float, float] | None = None
) -> KernelBundle:
    by_name: dict[str, dict] = {}
    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    dur_by_corr: dict[str, list[float]] = defaultdict(list)
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
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
            count_total += 1
            start = _numf(d.get("start_timestamp"))
            if not _in_window(start, window):
                continue
            kid = d.get("kernel_id")
            name = str(d.get("name") or names.get(kid) or f"kernel_{kid}").strip()
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
        count_total=count_total,
        source="json",
    )


def _build_api_bundle_from_json(
    files: list[Path], window: tuple[float, float] | None = None
) -> ApiBundle:
    by_name: dict[str, dict] = {}
    direct_launches: list[tuple[str, float]] = []
    graph_launches: list[tuple[str, float]] = []
    sync_count = 0
    sync_total_ns = 0.0
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
    for path in files:
        root = _json_root(_load_json(path))
        for d in _json_records(root, "hip_api"):
            count_total += 1
            start = _numf(d.get("start_timestamp"))
            if not _in_window(start, window):
                continue
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
        count_total=count_total,
        source="json",
    )


def _build_memcpy_bundle_from_json(
    files: list[Path], window: tuple[float, float] | None = None
) -> MemcpyBundle:
    by_dir: dict[str, dict] = {}
    bytes_available = False
    window_start = float("inf")
    window_end = float("-inf")
    count = 0
    count_total = 0
    for path in files:
        root = _json_root(_load_json(path))
        for d in _json_records(root, "memory_copy"):
            count_total += 1
            start = _numf(d.get("start_timestamp"))
            if not _in_window(start, window):
                continue
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
        count_total=count_total,
    )


def _kernel_bundle_from_stats(disc: DiscoveredReport) -> KernelBundle:
    """Pre-aggregated ``*_kernel_stats.csv`` fallback: no per-event timestamps to window by.

    ``count``/``count_total`` stay 0 (not the call total): several callers
    (``cmd_idle_gaps``, ``cmd_graphs``, ``cmd_host_idle``) key off
    ``bundle.count`` truthiness to detect "no per-event timestamps
    available" and fall back to a coarser, aggregate-only code path; window
    filtering is meaningless here for the same reason (no per-event
    timestamps to filter by).
    """
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


def _get_kernel_bundle(
    disc: DiscoveredReport, window: tuple[float, float] | None = None
) -> KernelBundle:
    if disc.kernel_trace:
        cache_key = ("kernel", _files_key(disc.kernel_trace), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_kernel_bundle_from_csv(disc.kernel_trace, window)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("kernel_json", _files_key(disc.json_files), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_kernel_bundle_from_json(disc.json_files, window)
        return _BUNDLE_CACHE[cache_key]
    return _kernel_bundle_from_stats(disc)


def _get_api_bundle(disc: DiscoveredReport, window: tuple[float, float] | None = None) -> ApiBundle:
    if disc.hip_api_trace:
        cache_key = ("api", _files_key(disc.hip_api_trace), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_api_bundle_from_csv(disc.hip_api_trace, window)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("api_json", _files_key(disc.json_files), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_api_bundle_from_json(disc.json_files, window)
        return _BUNDLE_CACHE[cache_key]
    return ApiBundle()


def _get_memcpy_bundle(
    disc: DiscoveredReport, window: tuple[float, float] | None = None
) -> MemcpyBundle:
    if disc.memory_copy_trace:
        cache_key = ("memcpy", _files_key(disc.memory_copy_trace), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_memcpy_bundle_from_csv(disc.memory_copy_trace, window)
        return _BUNDLE_CACHE[cache_key]
    if disc.json_files:
        cache_key = ("memcpy_json", _files_key(disc.json_files), window)
        if cache_key not in _BUNDLE_CACHE:
            _BUNDLE_CACHE[cache_key] = _build_memcpy_bundle_from_json(disc.json_files, window)
        return _BUNDLE_CACHE[cache_key]
    return MemcpyBundle()


# ---------------------------------------------------------------------------
# Window resolution: 'load' / 'startup' / 'all' / explicit 'start_s:end_s'
# ---------------------------------------------------------------------------
#
# A whole-run system-trace capture of a server includes the server's own
# startup (weight load, warmup, KV-cache init, ...), which can dominate the
# trace (a real capture: 12 minutes / 1.4M kernel rows, most of it startup)
# and pollute the family/kernel tables with one-time setup work that has
# nothing to do with steady-state serving. capture_runtime.run_capture
# records CLOCK_MONOTONIC-domain timestamps bracketing the "load phase"
# (ready_command succeeded -> load_command finished) and the whole capture
# (process launch -> capture end) in the manifest; rocprofv3's own CSV trace
# timestamps are CLOCK_MONOTONIC-based ns too (verified against real MI210
# fixtures: magnitude matches plausible system uptime, many orders of
# magnitude below a CLOCK_REALTIME epoch value), so both are directly
# comparable with no offset math *when they came from the same host/boot*.
# The functions below turn a requested window name into concrete
# CLOCK_MONOTONIC-ns bounds, defaulting to 'load' and falling back to 'all'
# whenever the manifest is missing, has no load window, or its own capture
# span doesn't plausibly bracket the trace's timestamps (different
# host/boot, or a synthetic/foreign fixture) -- never silently mis-slicing.

WINDOW_ALL = "all"
WINDOW_LOAD = "load"
WINDOW_STARTUP = "startup"
_DEFAULT_WINDOW = WINDOW_LOAD
# Generous: a profiler's own post-stop flush, or the outer capture
# lifecycle's escalation wait, can add real seconds-to-minutes between the
# recorded capture_start/capture_end stamps and the first/last event
# rocprofv3 actually wrote to disk.
_ALIGNMENT_SLACK_S = 300.0
_EXPLICIT_WINDOW_RE = re.compile(r"^\s*(-?[0-9.]+)\s*:\s*(-?[0-9.]+)\s*$")
_MANIFEST_NAME = "manifest.json"
_MANIFEST_SEARCH_DEPTH = 4


@dataclass(frozen=True)
class ResolvedWindow:
    """The outcome of resolving a ``window`` argument against one report.

    ``bounds`` is ``None`` for the 'all' window (no filtering) or whenever
    resolution fell back to it; otherwise a ``(start_ns, end_ns)`` pair in
    the same CLOCK_MONOTONIC-ns domain as the trace's own timestamps.
    ``label`` is the human-readable window name for the output header;
    ``note`` explains a fallback or is empty when the requested window
    resolved cleanly.
    """

    bounds: tuple[float, float] | None
    label: str
    note: str = ""


def _load_capture_manifest(report: str) -> dict[str, Any] | None:
    """Best-effort ``manifest.json`` lookup near *report*, or ``None``.

    ``report`` is almost always a capture's top-level directory (where
    ``capture_runtime.run_capture`` writes ``manifest.json``), reached via
    ``capture.resolve_report_arg`` before this module ever sees it. A
    bounded walk up a few parent directories also covers a *report* pointing
    at a nested ``<hostname>/<pid>`` file inside one. Returns ``None``
    (never raises) for a hand-run rocprofv3 capture, a test fixture, or any
    other report with no manifest -- window resolution then falls back to
    'all'.
    """
    path = Path(report)
    start = path if path.is_dir() else path.parent
    candidates = [start, *list(start.parents)[:_MANIFEST_SEARCH_DEPTH]]
    for candidate in candidates:
        manifest_path = candidate / _MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _clock_ns(stamp: object) -> float | None:
    """Extract a CLOCK_MONOTONIC-ns reading from one of capture_runtime's timestamp snapshots."""
    if not isinstance(stamp, dict):
        return None
    value = stamp.get("clock_monotonic_ns", stamp.get("monotonic_ns"))
    return float(value) if isinstance(value, (int, float)) else None


def _manifest_phase_bounds(manifest: dict[str, Any], kind: str) -> tuple[float, float] | None:
    """The requested named phase's ``(start_ns, end_ns)`` from *manifest*, or ``None``."""
    load_window = manifest.get("load_window")
    if not isinstance(load_window, dict):
        return None
    if kind == WINDOW_LOAD:
        start = _clock_ns(load_window.get("start"))
        end = _clock_ns(load_window.get("end"))
    else:  # WINDOW_STARTUP: process launch -> load phase start (or ready, if no load start)
        start = _clock_ns(manifest.get("capture_start"))
        end = _clock_ns(load_window.get("start")) or _clock_ns(load_window.get("ready"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _alignment_ok(manifest: dict[str, Any], trace_span: tuple[float, float]) -> bool:
    """Whether *trace_span* plausibly falls inside this manifest's own recorded capture span.

    A generous ``_ALIGNMENT_SLACK_S`` absorbs real flush/escalation delay
    between ``capture_end`` and the last byte rocprofv3 actually wrote.
    Failing this check means the trace's timestamps came from a different
    clock domain (a different host/boot, or a synthetic/foreign fixture
    reused against a live manifest) -- slicing by the manifest's window
    would silently misattribute events, so callers must fall back to 'all'
    instead.
    """
    cap_start = _clock_ns(manifest.get("capture_start"))
    cap_end = _clock_ns(manifest.get("capture_end"))
    if cap_start is None or cap_end is None:
        return False
    slack_ns = _ALIGNMENT_SLACK_S * _NS_PER_SEC
    trace_start, trace_end = trace_span
    return trace_start >= cap_start - slack_ns and trace_end <= cap_end + slack_ns


def _resolve_named_window(
    requested: str,
    manifest: dict[str, Any] | None,
    trace_span: tuple[float, float] | None,
) -> ResolvedWindow:
    """Resolve the ``'load'``/``'startup'`` branch of ``resolve_window``.

    Split out to keep each function's return-statement count within the
    repo's complexity ratchet.
    """
    label = "load phase" if requested == WINDOW_LOAD else "startup phase"
    if manifest is None:
        return ResolvedWindow(None, WINDOW_ALL, "no manifest.json found for this capture")
    bounds = _manifest_phase_bounds(manifest, requested)
    if bounds is None:
        return ResolvedWindow(None, WINDOW_ALL, f"no {label} recorded in this capture's manifest")
    if trace_span is None:
        return ResolvedWindow(None, WINDOW_ALL, "no timestamped trace data to slice")
    if not _alignment_ok(manifest, trace_span):
        return ResolvedWindow(
            None,
            WINDOW_ALL,
            f"trace timestamps do not align with the manifest's recorded capture span "
            f"(different host/boot, or a non-live fixture); refusing to risk mis-slicing the "
            f"{label}",
        )
    return ResolvedWindow(bounds, label)


def resolve_window(
    window: str | None,
    manifest: dict[str, Any] | None,
    trace_bundle: KernelBundle,
) -> ResolvedWindow:
    """Turn a requested ``window`` string into concrete bounds against one report.

    ``window`` is ``None``/``'load'`` (default: the recorded load phase),
    ``'all'`` (no filtering), ``'startup'`` (process launch through load
    start), or an explicit ``'start_s:end_s'`` pair of seconds relative to
    the trace's own earliest timestamp. Falls back to 'all' -- with ``note``
    explaining why -- whenever the requested window can't be resolved
    cleanly: no manifest, no recorded window, or the trace's timestamps
    don't plausibly align with the manifest's own recorded capture span.
    """
    requested = (window or _DEFAULT_WINDOW).strip().lower()
    trace_span = (
        (trace_bundle.window_start, trace_bundle.window_end) if trace_bundle.count_total else None
    )

    if requested == WINDOW_ALL:
        return ResolvedWindow(None, WINDOW_ALL)

    explicit = _EXPLICIT_WINDOW_RE.match(requested)
    if explicit:
        if trace_span is None:
            return ResolvedWindow(
                None, WINDOW_ALL, "no timestamped trace data to anchor an explicit window against"
            )
        start_s, end_s = float(explicit.group(1)), float(explicit.group(2))
        base = trace_span[0]
        return ResolvedWindow(
            (base + start_s * _NS_PER_SEC, base + end_s * _NS_PER_SEC),
            f"custom range {start_s:g}s-{end_s:g}s from trace start",
        )

    if requested not in (WINDOW_LOAD, WINDOW_STARTUP):
        return ResolvedWindow(
            None,
            WINDOW_ALL,
            f"unknown window {window!r} (use 'load', 'startup', 'all', or 'start_s:end_s')",
        )

    return _resolve_named_window(requested, manifest, trace_span)


def _resolve_window_for_report(
    disc: DiscoveredReport, report: str, window: str | None
) -> ResolvedWindow:
    manifest = _load_capture_manifest(report)
    unfiltered = _get_kernel_bundle(disc)
    return resolve_window(window, manifest, unfiltered)


def _window_header(
    resolved: ResolvedWindow, kept: int, total: int, *, unit: str = "dispatches"
) -> str:
    """The ``window: ...`` line every windowed subcommand prints first."""
    line = f"window: {resolved.label}, {kept} of {total} {unit}"
    if resolved.bounds is not None:
        line += "; pass window='all' for the whole run"
    lines = [line]
    if resolved.note:
        lines.append(f"  ({resolved.note})")
    return "\n".join(lines)


def _load_kernels(
    disc: DiscoveredReport, window: tuple[float, float] | None = None
) -> tuple[dict[str, dict], str]:
    """Return (per-kernel aggregate, source label): 'trace', 'json', 'stats', or ''."""
    bundle = _get_kernel_bundle(disc, window)
    return bundle.by_name, bundle.source


def kernel_time_totals(report: str, window: str | None = "load") -> dict[str, float]:
    """Per-kernel total GPU time in nanoseconds, keyed by kernel name.

    Thin, structured counterpart to ``cmd_kernels``'s printed table, for
    callers (``compare``) that need numeric deltas rather than formatted
    text. Returns an empty dict when *report* has no kernel data.

    ``window`` has the same meaning as every windowed subcommand's
    ``--window`` (default ``'load'``: the capture's recorded load phase
    when available, falling back to ``'all'`` otherwise -- see
    ``resolve_window``).
    """
    disc = discover(report)
    resolved = _resolve_window_for_report(disc, report, window)
    agg, _source = _load_kernels(disc, resolved.bounds)
    return {name: entry["total_ns"] for name, entry in agg.items()}


def family_time_totals(report: str, window: str | None = "load") -> dict[str, float]:
    """Per-library-family total GPU time in nanoseconds.

    Same family classification as ``cmd_families``, structured for
    ``compare`` rather than printed. ``window`` is forwarded to
    ``kernel_time_totals``.
    """
    totals: dict[str, float] = {}
    for name, total_ns in kernel_time_totals(report, window=window).items():
        family = _classify_family(name)
        totals[family] = totals.get(family, 0.0) + total_ns
    return totals


# ---------------------------------------------------------------------------
# Subcommand: files  # noqa: ERA001  # LW-920009; this is a section-header comment formatted like a heading, not commented-out code
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
    print("\nDiscovered CSV files:")  # noqa: T201  # LW-920010; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for label, files in buckets:
        if not files:
            continue
        any_found = True
        rows = sum(_csv_row_count(f) for f in files)
        print(f"  {label:<20s}: {len(files)} file(s), {rows} row(s)")  # noqa: T201  # LW-920011; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for label, files in (("json", disc.json_files), ("rocpd sqlite (.db)", disc.db_files)):
        if files:
            any_found = True
            print(f"  {label:<20s}: {len(files)} file(s)")  # noqa: T201  # LW-920012; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if disc.other_csv:
        print(f"  {'other .csv':<20s}: {len(disc.other_csv)} file(s) (unrecognized — not analyzed)")  # noqa: T201  # LW-920013; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if not any_found:
        print("  (none found)")  # noqa: T201  # LW-920014; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    return any_found


def cmd_files(ns: argparse.Namespace) -> None:
    """Discover output files, process captures, agents, and row counts."""
    disc = discover(ns.report)
    print(f"Report root: {disc.root}")  # noqa: T201  # LW-920015; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    if not _print_file_buckets(disc):
        print(f"\n(no rocprofv3 output recognized under {disc.root})")  # noqa: T201  # LW-920016; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        return

    process_dirs = _process_dirs(disc)
    if process_dirs:
        print("\nProcess captures:")  # noqa: T201  # LW-920017; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for d in process_dirs:
            print(f"  {_describe_process_dir(d, disc.root)}")  # noqa: T201  # LW-920018; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    agents = _load_agents(disc)
    if agents:
        print("\nGPU/CPU agents:")  # noqa: T201  # LW-920019; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for a in agents:
            print(f"  agent {a['agent']:<6s} {a['name']:<24s} gfx={a['gfx']:<12s} type={a['type']}")  # noqa: T201  # LW-920020; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    print("\nStart with `kernels`, `families`, or `summary`.")  # noqa: T201  # LW-920021; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


# ---------------------------------------------------------------------------
# Subcommand: kernels  # noqa: ERA001  # LW-920022; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------


def _gpu_busy_denominator_ns(bundle: KernelBundle, naive_sum_ns: float) -> float:
    """True GPU-busy denominator for a "%GPU" share.

    Merged-interval union across every (agent, queue), not a naive sum of
    per-kernel durations: the naive sum double-counts whenever kernels on
    different HW queues of the same GPU genuinely overlap in wall-clock time
    (real, if usually small, on rocprofv3 serving-workload captures with
    concurrent queues).
    ``idle_gaps``/``host_idle`` already back their busy-time accounting with
    this same merged union (``_kernel_union_ns``); ``kernels``/``families``
    now use it too instead of a bespoke sum that only agrees with it when
    there happens to be no cross-queue overlap. Falls back to the naive sum
    when only pre-aggregated ``*_kernel_stats.csv`` is available (no
    per-event timestamps to merge).
    """
    if bundle.by_key:
        return _kernel_union_ns(bundle)
    return naive_sum_ns


def _print_overlap_note(naive_sum_ns: float, denom_ns: float) -> None:
    """Flag when %GPU's merged-busy denominator diverges materially from the naive sum."""
    overlap_ns = naive_sum_ns - denom_ns
    if overlap_ns > max(1e6, naive_sum_ns * 0.005):
        print(  # noqa: T201  # LW-920023; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            f"Note: {_fmt_ns(overlap_ns)} of the summed kernel time above overlaps across HW "
            f"queues on the same GPU; %GPU below is computed against the merged busy time "
            f"({_fmt_ns(denom_ns)}), not the naive per-kernel sum."
        )


def cmd_kernels(ns: argparse.Namespace) -> None:
    """Top GPU kernels by total execution time, with a library-family column."""
    disc = discover(ns.report)
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    bundle = _get_kernel_bundle(disc, resolved.bounds)
    agg, source = bundle.by_name, bundle.source
    if not agg:
        print(  # noqa: T201  # LW-920024; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "(no kernel data found — no *_kernel_trace.csv or *_kernel_stats.csv under report path)"
        )
        return

    total_ns = sum(e["total_ns"] for e in agg.values())
    total_calls = sum(e["calls"] for e in agg.values())
    denom_ns = _gpu_busy_denominator_ns(bundle, total_ns)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["total_ns"])[: ns.top]

    print(_window_header(resolved, bundle.count, bundle.count_total))  # noqa: T201  # LW-920025; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Kernel data source: {source}")  # noqa: T201  # LW-920026; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    header = f"{'Kernel':<44s} {'Family':<28s} {'Calls':>7s} {'Total':>9s} {'Avg':>9s} {'Min':>9s} {'Max':>9s} {'%GPU':>6s}"
    print(header)  # noqa: T201  # LW-920027; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print("-" * len(header))  # noqa: T201  # LW-920028; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for name, e in ranked:
        fam = _classify_family(name)
        avg = e["total_ns"] / e["calls"] if e["calls"] else 0.0
        pct = e["total_ns"] / denom_ns * 100 if denom_ns else 0.0
        min_ns = e["min_ns"] if e["min_ns"] != float("inf") else 0.0
        print(  # noqa: T201  # LW-920029; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            f"{_truncate(_short_name(name), 44):<44s} {fam:<28s} {e['calls']:>7d} {_fmt_ns(e['total_ns']):>9s} "
            f"{_fmt_ns(avg):>9s} {_fmt_ns(min_ns):>9s} {_fmt_ns(e['max_ns']):>9s} {pct:>5.1f}%"
        )
    print(f"\nTotal GPU kernel time (all kernels): {_fmt_ns(total_ns)}")  # noqa: T201  # LW-920030; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Total kernel launches: {total_calls}")  # noqa: T201  # LW-920031; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    _print_overlap_note(total_ns, denom_ns)


# ---------------------------------------------------------------------------
# Subcommand: families  # noqa: ERA001  # LW-920032; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------


def _outlier_family_note(ordered: list[tuple[str, dict]]) -> str | None:
    """Flag when one family's average per-call duration dwarfs every other family's.

    Caught on a real MI210 graph-mode serving-workload trace: Composable Kernel's
    FmhaFwdKernel averaged 338ms/call (27 calls, 31% of GPU time) while every
    other family on the same capture averaged tens to hundreds of
    *microseconds* per call -- a 1000x+ outlier that rocprofv3's own
    kernel_stats.csv corroborated (not a VibeSys aggregation bug: raw
    Start/End_Timestamp rows for that kernel are sequential, non-overlapping,
    and back-to-back with its neighbors). A gap this large from every peer
    family is real signal (severe kernel-selection/occupancy trouble, e.g. a
    grouped/varlen attention kernel launched with a badly undersized grid for
    the batch) or, less likely, a rocprofv3 dispatch-timing quirk specific to
    that kernel's launch shape -- either way, not something to repeat as a
    literal %GPU wall-clock share without a targeted follow-up capture.
    """
    avgs = [(fam, e["total_ns"] / e["calls"]) for fam, e in ordered if e["calls"] > 0]
    if len(avgs) < 2:  # noqa: PLR2004  # LW-920033; this is a well-known, self-explanatory constant from the file format/tool being parsed
        return None
    avgs.sort(key=lambda kv: -kv[1])
    top_fam, top_avg = avgs[0]
    rest = sorted(avg for _fam, avg in avgs[1:])
    median_rest = rest[len(rest) // 2]
    if median_rest <= 0 or top_avg <= median_rest * _OUTLIER_AVG_MULTIPLE:
        return None
    return (
        f"\n*** Finding: '{top_fam}' averages {_fmt_ns(top_avg)}/call, "
        f"{top_avg / median_rest:.0f}x the median family's per-call average "
        f"({_fmt_ns(median_rest)}/call). Its %GPU share above reflects rocprofv3's raw "
        f"measurement, not a VibeSys computation error -- but before citing it as real "
        f"GPU-bound work, verify with `compute.py profile`/`att.py` on that kernel: this size "
        f"of gap usually means a badly undersized launch grid or a kernel-selection problem, "
        f"and occasionally a capture artifact specific to that kernel's launch shape. ***"
    )


def cmd_families(ns: argparse.Namespace) -> None:
    """GPU time grouped by kernel-library family, flagging fallback-family hot spots."""
    disc = discover(ns.report)
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    bundle = _get_kernel_bundle(disc, resolved.bounds)
    agg, source = bundle.by_name, bundle.source
    if not agg:
        print(  # noqa: T201  # LW-920034; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
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
    denom_ns = _gpu_busy_denominator_ns(bundle, total_ns)
    ordered = sorted(fam_totals.items(), key=lambda kv: -kv[1]["total_ns"])

    print(_window_header(resolved, bundle.count, bundle.count_total))  # noqa: T201  # LW-920035; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Kernel data source: {source}")  # noqa: T201  # LW-920036; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    header = (
        f"{'Family':<32s} {'Calls':>8s} {'Total':>10s} {'%GPU':>6s}  {'Top kernel in family':<40s}"
    )
    print(header)  # noqa: T201  # LW-920037; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print("-" * len(header))  # noqa: T201  # LW-920038; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for fam, e in ordered:
        pct = e["total_ns"] / denom_ns * 100 if denom_ns else 0.0
        print(  # noqa: T201  # LW-920039; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            f"{fam:<32s} {e['calls']:>8d} {_fmt_ns(e['total_ns']):>10s} {pct:>5.1f}%  "
            f"{_truncate(_short_name(e['top_name']), 40):<40s}"
        )
    _print_overlap_note(total_ns, denom_ns)

    flags = [
        (fam, e["top_name"], e["total_ns"] / denom_ns * 100 if denom_ns else 0.0)
        for fam, e in ordered
        if fam in FALLBACK_FAMILIES
        and e["total_ns"] > 0
        and (e["total_ns"] / denom_ns * 100 if denom_ns else 0.0)
        >= _FALLBACK_FAMILY_SHARE_THRESHOLD
        and _GEMM_ATTN_RE.search(e["top_name"])
    ]
    if flags:
        print(  # noqa: T201  # LW-920040; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "\n*** Finding: GEMM/attention-shaped work is landing in a fallback family "
            "instead of AITER/CK/hipBLASLt ***"
        )
        for fam, top_name, share in flags:
            print(  # noqa: T201  # LW-920041; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                f"  {fam}: '{_short_name(top_name)}' — {share:.1f}% of GPU time. Check "
                f"dispatch/tuning config (e.g. AITER_LOG_TUNED_CONFIG=1) instead of accepting "
                f"the fallback kernel."
            )

    outlier_note = _outlier_family_note(ordered)
    if outlier_note:
        print(outlier_note)  # noqa: T201  # LW-920042; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


# ---------------------------------------------------------------------------
# Subcommand: idle_gaps  # noqa: ERA001  # LW-920043; this is a section-header comment formatted like a heading, not commented-out code
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
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    bundle = _get_kernel_bundle(disc, resolved.bounds)
    if bundle.count < 2:  # noqa: PLR2004  # LW-920044; this is a well-known, self-explanatory constant from the file format/tool being parsed
        if disc.kernel_stats and not disc.kernel_trace:
            print(  # noqa: T201  # LW-920045; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                "(idle-gap analysis needs per-event timestamps from *_kernel_trace.csv; "
                "only aggregate *_kernel_stats.csv was found.)"
            )
        else:
            print("(fewer than 2 kernel events with timestamps found.)")  # noqa: T201  # LW-920046; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
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

    print(_window_header(resolved, bundle.count, bundle.count_total))  # noqa: T201  # LW-920047; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Capture window: {_fmt_ns(window_ns)}")  # noqa: T201  # LW-920048; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"GPU busy (union per agent/queue): {_fmt_ns(total_busy)}")  # noqa: T201  # LW-920049; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    denom = total_busy + total_idle
    pct_idle = total_idle / denom * 100 if denom else 0.0
    print(f"GPU idle (intra-key gaps > 1us): {_fmt_ns(total_idle)} ({pct_idle:.1f}%)")  # noqa: T201  # LW-920050; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Idle gaps found: {len(gaps)}")  # noqa: T201  # LW-920051; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    top = gaps[: ns.top]
    if top:
        print(f"\nTop {len(top)} gaps:")  # noqa: T201  # LW-920052; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(f"  {'Agent/Queue':<16s} {'Gap':>10s}  {'After':<32s} -> {'Before':<32s}")  # noqa: T201  # LW-920053; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print("  " + "-" * 92)  # noqa: T201  # LW-920054; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for (agent, queue), prev_name, next_name, gap in top:
            key_str = f"{agent}/{queue}"
            print(  # noqa: T201  # LW-920055; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                f"  {key_str:<16s} {_fmt_ns(gap):>10s}  {_truncate(_short_name(prev_name), 32):<32s} -> "
                f"{_truncate(_short_name(next_name), 32):<32s}"
            )


# ---------------------------------------------------------------------------
# Subcommand: cpu_overhead  # noqa: ERA001  # LW-920056; this is a section-header comment formatted like a heading, not commented-out code
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
    print(f"\n{'API':<36s} {'Calls':>8s} {'Total':>10s} {'Avg':>10s}")  # noqa: T201  # LW-920057; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print("-" * 68)  # noqa: T201  # LW-920058; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    for name, e in ranked:
        avg = e["total_ns"] / e["calls"] if e["calls"] else 0.0
        print(  # noqa: T201  # LW-920059; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
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
        print(  # noqa: T201  # LW-920060; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "\n(no matching Correlation_Id between HIP API and kernel trace — "
            "cannot compute a matched launch-bound ratio)"
        )
        return

    avg_cpu = sum(matched_cpu) / len(matched_cpu)
    avg_gpu = sum(matched_gpu) / len(matched_gpu)
    print(f"\nKernel launch overhead ({len(matched_cpu)} matched by Correlation_Id):")  # noqa: T201  # LW-920061; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"  Avg CPU launch: {_fmt_ns(avg_cpu)}")  # noqa: T201  # LW-920062; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"  Avg GPU exec:   {_fmt_ns(avg_gpu)}")  # noqa: T201  # LW-920063; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if avg_gpu > 0:
        ratio = avg_cpu / avg_gpu
        print(f"  CPU/GPU ratio:  {ratio:.2f}x")  # noqa: T201  # LW-920064; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        if ratio > _LAUNCH_BOUND_RATIO_THRESHOLD:
            print("  *** LAUNCH-BOUND — CPU launch overhead exceeds GPU execution time ***")  # noqa: T201  # LW-920065; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


def _cpu_overhead_from_trace(api_bundle: ApiBundle, kernel_bundle: KernelBundle) -> None:
    total_calls = api_bundle.count
    total_ns = sum(e["total_ns"] for e in api_bundle.by_name.values())
    print(f"Total HIP API calls: {total_calls}")  # noqa: T201  # LW-920066; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Total CPU time in HIP APIs: {_fmt_ns(total_ns)}")  # noqa: T201  # LW-920067; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    _print_api_table(
        sorted(api_bundle.by_name.items(), key=lambda kv: -kv[1]["total_ns"])[:_TOP_API_ROWS]
    )

    print(  # noqa: T201  # LW-920068; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        f"\nSynchronization stalls: {api_bundle.sync_count} calls, {_fmt_ns(api_bundle.sync_total_ns)}"
    )

    launch_calls = api_bundle.direct_launches + api_bundle.graph_launches
    window_ns = api_bundle.window_end - api_bundle.window_start
    rate = len(launch_calls) / (window_ns / _NS_PER_SEC) if window_ns > 0 else 0.0
    print(  # noqa: T201  # LW-920069; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        f"\nKernel launch calls: {len(launch_calls)}  ({rate:.1f} launches/sec over the capture window)"
    )

    if kernel_bundle.dur_by_corr and launch_calls:
        _launch_bound_from_correlation(launch_calls, kernel_bundle.dur_by_corr)
    elif launch_calls:
        print(  # noqa: T201  # LW-920070; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "\n(no *_kernel_trace.csv found — cannot compute a matched CPU-launch-vs-GPU-exec ratio)"
        )


def _cpu_overhead_from_stats(stats: dict[str, dict], disc: DiscoveredReport) -> None:
    total_calls = sum(s["calls"] for s in stats.values())
    total_ns = sum(s["total_ns"] for s in stats.values())
    print(f"Total HIP API calls: {total_calls}  (aggregated from *_hip_api_stats.csv)")  # noqa: T201  # LW-920071; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Total CPU time in HIP APIs: {_fmt_ns(total_ns)}")  # noqa: T201  # LW-920072; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    _print_api_table(sorted(stats.items(), key=lambda kv: -kv[1]["total_ns"])[:_TOP_API_ROWS])

    launch_ns = sum(s["total_ns"] for n, s in stats.items() if _is_launch_api(n))
    launch_calls = sum(s["calls"] for n, s in stats.items() if _is_launch_api(n))
    kernel_agg, _src = _load_kernels(disc)
    kernel_ns = sum(e["total_ns"] for e in kernel_agg.values())
    print(f"\nKernel launch calls: {launch_calls}, total CPU time {_fmt_ns(launch_ns)}")  # noqa: T201  # LW-920073; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if kernel_ns > 0 and launch_ns > 0:
        ratio = launch_ns / kernel_ns
        print(  # noqa: T201  # LW-920074; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            f"Coarse CPU-launch-time / GPU-kernel-time ratio: {ratio:.2f}x (aggregate, not per-launch matched)"
        )
        if ratio > _LAUNCH_BOUND_RATIO_THRESHOLD:
            print("*** Possibly launch-bound — coarse launch time exceeds kernel GPU time ***")  # noqa: T201  # LW-920075; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


def cmd_cpu_overhead(ns: argparse.Namespace) -> None:
    """HIP API time, launch rate, sync stalls, and a launch-bound heuristic."""
    disc = discover(ns.report)
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    api_bundle = _get_api_bundle(disc, resolved.bounds)
    if api_bundle.count:
        print(_window_header(resolved, api_bundle.count, api_bundle.count_total, unit="API calls"))  # noqa: T201  # LW-920076; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        _cpu_overhead_from_trace(api_bundle, _get_kernel_bundle(disc, resolved.bounds))
        return
    stats = _load_api_stats(disc)
    if not stats:
        print(  # noqa: T201  # LW-920077; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "(no HIP API data found — no *_hip_api_trace.csv or *_hip_api_stats.csv under report path)"
        )
        return
    _cpu_overhead_from_stats(stats, disc)


# ---------------------------------------------------------------------------
# Subcommand: memory  # noqa: ERA001  # LW-920078; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------


def cmd_memory(ns: argparse.Namespace) -> None:
    """Memory copies by direction, bytes, time, and bandwidth."""
    disc = discover(ns.report)
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    bundle = _get_memcpy_bundle(disc, resolved.bounds)
    if not bundle.count:
        print("(no memory copy data found — no *_memory_copy_trace.csv under report path)")  # noqa: T201  # LW-920079; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        return

    print(_window_header(resolved, bundle.count, bundle.count_total, unit="memory copies"))  # noqa: T201  # LW-920080; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    ordered = sorted(bundle.by_dir.items(), key=lambda kv: -kv[1]["total_ns"])
    if bundle.bytes_available:
        print(f"{'Direction':<10s} {'Count':>8s} {'Total':>10s} {'Bytes':>16s} {'Bandwidth':>12s}")  # noqa: T201  # LW-920081; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print("-" * 60)  # noqa: T201  # LW-920082; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for d, e in ordered:
            bw_gbps = (
                (e["bytes"] / _BYTES_PER_GB) / (e["total_ns"] / _NS_PER_SEC)
                if e["total_ns"] > 0
                else 0.0
            )
            print(  # noqa: T201  # LW-920083; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                f"{d:<10s} {e['count']:>8d} {_fmt_ns(e['total_ns']):>10s} {e['bytes']:>16,.0f} {bw_gbps:>10.1f}GB/s"
            )
        total_bytes = sum(e["bytes"] for e in bundle.by_dir.values())
        total_ns = sum(e["total_ns"] for e in bundle.by_dir.values())
        print(f"\nTotal memory copy: {total_bytes / _BYTES_PER_GB:.2f} GB in {_fmt_ns(total_ns)}")  # noqa: T201  # LW-920084; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    else:
        print(f"{'Direction':<10s} {'Count':>8s} {'Total':>10s}")  # noqa: T201  # LW-920085; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print("-" * 34)  # noqa: T201  # LW-920086; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        for d, e in ordered:
            print(f"{d:<10s} {e['count']:>8d} {_fmt_ns(e['total_ns']):>10s}")  # noqa: T201  # LW-920087; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        total_ns = sum(e["total_ns"] for e in bundle.by_dir.values())
        print(f"\nTotal memory copy time: {_fmt_ns(total_ns)}")  # noqa: T201  # LW-920088; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(  # noqa: T201  # LW-920089; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "(byte counts not available: this rocprofv3 capture's "
            "*_memory_copy_trace.csv has no Bytes/Size column — showing count and time only)"
        )


# ---------------------------------------------------------------------------
# Subcommand: graphs  # noqa: ERA001  # LW-920090; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------

_GRAPH_DEGRADED_KERNELS_PER_LAUNCH = 3.0
_GRAPH_DEGRADED_DIRECT_LAUNCH_FRACTION = 0.5


def _report_graph_counts(graph_calls: int, direct_calls: int, total_kernels: int) -> None:
    print(f"hipGraphLaunch calls: {graph_calls}")  # noqa: T201  # LW-920091; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Direct launch calls (hipLaunchKernel/hipModuleLaunchKernel/...): {direct_calls}")  # noqa: T201  # LW-920092; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Total kernels recorded: {total_kernels}")  # noqa: T201  # LW-920093; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if graph_calls == 0:
        print(  # noqa: T201  # LW-920094; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "\n(No HIP graph launches detected — per-kernel attribution via direct "
            "launch APIs should be reliable.)"
        )
        return

    kernels_per_launch = total_kernels / graph_calls
    print(f"\nKernels per graph launch (coarse): {kernels_per_launch:.1f}")  # noqa: T201  # LW-920095; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    degraded = (
        direct_calls < total_kernels * _GRAPH_DEGRADED_DIRECT_LAUNCH_FRACTION
        or kernels_per_launch > _GRAPH_DEGRADED_KERNELS_PER_LAUNCH
    )
    if degraded:
        print(  # noqa: T201  # LW-920096; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
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
    print(f"\nGraph launches with matched kernels (by Correlation_Id): {len(matched)}")  # noqa: T201  # LW-920097; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(  # noqa: T201  # LW-920098; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        f"  Avg kernels per launch: {sum(counts) / len(counts):.1f}  (min {min(counts)}, max {max(counts)})"
    )
    print(f"  Avg GPU time per launch: {_fmt_ns(sum(times) / len(times))}")  # noqa: T201  # LW-920099; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


def cmd_graphs(ns: argparse.Namespace) -> None:
    """HIP graph launches, and detection of graph-degraded kernel attribution."""
    disc = discover(ns.report)
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    kernel_bundle = _get_kernel_bundle(disc, resolved.bounds)
    total_kernels = kernel_bundle.count or sum(e["calls"] for e in kernel_bundle.by_name.values())

    api_bundle = _get_api_bundle(disc, resolved.bounds)
    print(_window_header(resolved, kernel_bundle.count, kernel_bundle.count_total))  # noqa: T201  # LW-920100; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    if api_bundle.count:
        _report_graph_counts(
            len(api_bundle.graph_launches), len(api_bundle.direct_launches), total_kernels
        )
        if api_bundle.graph_launches and kernel_bundle.dur_by_corr:
            _report_matched_graph_kernels(api_bundle.graph_launches, kernel_bundle.dur_by_corr)
        return

    stats = _load_api_stats(disc)
    if not stats:
        print("(no HIP API data found — cannot detect HIP graph launches)")  # noqa: T201  # LW-920101; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        return
    graph_calls = sum(s["calls"] for n, s in stats.items() if _is_graph_launch_api(n))
    direct_calls = sum(
        s["calls"] for n, s in stats.items() if _is_launch_api(n) and not _is_graph_launch_api(n)
    )
    _report_graph_counts(graph_calls, direct_calls, total_kernels)


# ---------------------------------------------------------------------------
# Subcommand: host_idle  # noqa: ERA001  # LW-920102; this is a section-header comment formatted like a heading, not commented-out code
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
    resolved = _resolve_window_for_report(disc, ns.report, getattr(ns, "window", None))
    kernel_bundle = _get_kernel_bundle(disc, resolved.bounds)
    api_bundle = _get_api_bundle(disc, resolved.bounds)
    mem_bundle = _get_memcpy_bundle(disc, resolved.bounds)

    bundles = (kernel_bundle, api_bundle, mem_bundle)
    total_events = sum(b.count for b in bundles)
    if not total_events:
        if kernel_bundle.by_name:
            print(  # noqa: T201  # LW-920103; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
                "(only aggregate stats found — no per-event timestamps to compute a capture "
                "window; cannot run the host-idle check. Use `kernels`/`families` instead.)"
            )
        else:
            print("(no timestamped data found at all — nothing to check.)")  # noqa: T201  # LW-920104; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        return

    starts = [b.window_start for b in bundles if b.count]
    ends = [b.window_end for b in bundles if b.count]
    window_ns = max(ends) - min(starts) if starts else 0.0
    busy_ns = _kernel_union_ns(kernel_bundle)
    busy_pct = busy_ns / window_ns * 100 if window_ns > 0 else 0.0

    print(_window_header(resolved, kernel_bundle.count, kernel_bundle.count_total))  # noqa: T201  # LW-920105; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Capture window: {_fmt_ns(window_ns)}")  # noqa: T201  # LW-920106; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"GPU kernel activity: {_fmt_ns(busy_ns)} ({busy_pct:.2f}% of window)")  # noqa: T201  # LW-920107; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print(f"Kernels recorded: {kernel_bundle.count}")  # noqa: T201  # LW-920108; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

    if not kernel_bundle.count or busy_pct < _HOST_IDLE_BUSY_PCT_THRESHOLD:
        print(  # noqa: T201  # LW-920109; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "\n*** VERDICT: trace looks mostly HOST-IDLE — the GPU did little or no work "
            "during the capture window. The capture likely missed the load (started before "
            "or after the workload ran) or the server was idle. Recommendation: re-capture "
            "under active load, or widen the capture window to cover the workload's steady "
            "state. ***"
        )
    else:
        print(  # noqa: T201  # LW-920110; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            f"\nVerdict: GPU active for {busy_pct:.1f}% of the capture window — trace looks valid."
        )


# ---------------------------------------------------------------------------
# Subcommand: query  # noqa: ERA001  # LW-920111; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------


def cmd_query(ns: argparse.Namespace) -> None:
    """Run arbitrary SQL against a rocpd SQLite export.

    Only works against a rocpd SQLite (``.db``) export: ROCm 7+'s
    ``rocprofv3 ... --output-format rocpd``, converted or exported to
    SQLite. It does **not** work against the CSV or ``--output-format
    json`` output every other subcommand here reads (there is no rocpd
    ``.db`` file to query in that case); use ``kernels``/``families``/
    ``summary``/etc. for those. Not windowed (unlike the trace subcommands
    above): the SQL is run as given against the whole rocpd export.
    """
    disc = discover(ns.report)
    if not disc.db_files:
        print(  # noqa: T201  # LW-920112; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            "(no rocpd SQLite (.db) file found under report path. `query` only works "
            "against a rocpd SQLite export -- ROCm 7+'s `rocprofv3 ... --output-format "
            "rocpd` -- not CSV or `--output-format json` output; use "
            "`kernels`/`families`/`summary`/etc. for those.)"
        )
        return
    conn = sqlite3.connect(str(disc.db_files[0]))
    try:
        cur = conn.execute(ns.sql)
        if cur.description:
            headers = [d[0] for d in cur.description]
            print("\t".join(headers))  # noqa: T201  # LW-920113; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
            for row in cur.fetchall():
                print("\t".join(str(v) for v in row))  # noqa: T201  # LW-920114; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        else:
            print("(no results)")  # noqa: T201  # LW-920115; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    except sqlite3.OperationalError as exc:
        print(f"SQL error: {exc}", file=sys.stderr)  # noqa: T201  # LW-920116; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        sys.exit(1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subcommand: summary  # noqa: ERA001  # LW-920117; this is a section-header comment formatted like a heading, not commented-out code
# ---------------------------------------------------------------------------

_SUMMARY_LINE_CAP = 40


def _capped(fn, ns: argparse.Namespace) -> str:  # noqa: ANN001  # LW-920118; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
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
    """All-in-one analysis: files, validity checks, kernels/families/idle/overhead/memory/graphs.

    Defaults to the ``window='load'`` slice (the capture's recorded load
    phase, when available) for every section below except ``Files``: each
    section prints its own ``window: ...`` header. Pass ``window='all'`` for
    the whole run.
    """
    ns.top = getattr(ns, "top", 15)
    ns.window = getattr(ns, "window", None)

    print("=" * 78)  # noqa: T201  # LW-920119; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print("  ROCPROFV3 TRACE SUMMARY")  # noqa: T201  # LW-920120; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
    print("=" * 78)  # noqa: T201  # LW-920121; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism

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
        print(f"\n## {title}\n")  # noqa: T201  # LW-920122; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism
        print(_capped(fn, ns))  # noqa: T201  # LW-920123; this standalone script reports progress/results/diagnostics on stdout or stderr, its intended output mechanism


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

    def _add_window_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--window",
            default=None,
            help=(
                "'load' (default, when the capture recorded a load phase) | 'all' | "
                "'startup' | explicit 'start_s:end_s' relative to trace start"
            ),
        )

    p = sub.add_parser("kernels", help="Top GPU kernels by total time")
    _add_report_arg(p)
    p.add_argument("--top", type=int, default=15)
    _add_window_arg(p)

    p = sub.add_parser("families", help="GPU time grouped by kernel library family")
    _add_report_arg(p)
    _add_window_arg(p)

    p = sub.add_parser("idle_gaps", help="GPU busy vs idle, largest gaps")
    _add_report_arg(p)
    p.add_argument("--top", type=int, default=10)
    _add_window_arg(p)

    p = sub.add_parser("cpu_overhead", help="HIP API launch overhead and launch-bound heuristic")
    _add_report_arg(p)
    _add_window_arg(p)

    p = sub.add_parser("memory", help="Memory copies by direction, bytes, bandwidth")
    _add_report_arg(p)
    _add_window_arg(p)

    p = sub.add_parser("graphs", help="HIP graph launches and attribution-degradation check")
    _add_report_arg(p)
    _add_window_arg(p)

    p = sub.add_parser("host_idle", help="Detect a mostly host-idle / load-missed capture")
    _add_report_arg(p)
    _add_window_arg(p)

    p = sub.add_parser(
        "query", help="Run arbitrary SQL against a rocpd SQLite export (ROCm 7+ only)"
    )
    _add_report_arg(p)
    p.add_argument("sql")

    p = sub.add_parser("summary", help="All-in-one analysis")
    _add_report_arg(p)
    _add_window_arg(p)
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
