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
  analysis.
- CSV stats files: ``*_kernel_stats.csv``, ``*_hip_api_stats.csv`` —
  pre-aggregated (name, calls, total/avg/min/max duration). Enough for
  ``kernels``/``families``/``cpu_overhead`` totals, but not for anything that
  needs per-event timestamps.
- ``*_domain_stats.csv`` / ``*_agent_info.csv`` — used by ``files``.
- rocprofv3 JSON output (``--output-format json``) — best-effort support for
  the documented ``buffer_records`` (``kernel_dispatch``, ``hip_api``,
  ``memory_copy``) shape, used only when no matching CSV is present.
- rocpd SQLite (``*.db``, ROCm 7) — only wired up for ``query``; every other
  subcommand explains that it needs CSV or JSON instead.

Column names vary across ROCm/rocprofv3 versions, so every reader resolves a
handful of candidate names per logical field instead of assuming one exact
header.

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
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

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
    if v is None:
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


def _classify_family(name: str) -> str:
    """Classify a kernel name into a library family (best-effort, name-only)."""
    lname = name.lower()
    for family, subs in _FAMILY_RULES:
        if any(s in lname for s in subs):
            return family
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
# CSV -> canonical row adapters
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


def _event_row(name: str, r: dict) -> dict:
    # Keep the full, unshortened name here: classification (``_classify_family``)
    # depends on namespace/library markers that ``_short_name`` may strip when a
    # name has more than two ``::``-separated segments. Shorten only at display
    # time (``_short_name`` is applied when printing a table row).
    start = _numf(_get(r, _START_COLS))
    end = _numf(_get(r, _END_COLS))
    dur = end - start if end > start else _numf(_get(r, _DUR_COLS))
    return {
        "name": name.strip(),
        "start_ns": start,
        "end_ns": end,
        "dur_ns": dur,
        "agent": _get(r, _AGENT_COLS) or "0",
        "queue": _get(r, _QUEUE_COLS) or "0",
        "corr": _get(r, _CORR_COLS) or "",
        "pid": _get(r, _PID_COLS) or "",
    }


def _kernel_rows_from_csv(path: Path) -> list[dict]:
    return [_event_row(_get(r, _KERNEL_NAME_COLS) or "?", r) for r in _read_csv_dicts(path)]


def _api_rows_from_csv(path: Path) -> list[dict]:
    return [_event_row(_get(r, _API_NAME_COLS) or "?", r) for r in _read_csv_dicts(path)]


def _memcpy_rows_from_csv(path: Path) -> list[dict]:
    rows = []
    for r in _read_csv_dicts(path):
        ev = _event_row("memcpy", r)
        rows.append(
            {
                "direction": _normalize_direction(_get(r, _MEMCPY_DIR_COLS) or ""),
                "start_ns": ev["start_ns"],
                "end_ns": ev["end_ns"],
                "dur_ns": ev["dur_ns"],
                "bytes": _numf(_get(r, _MEMCPY_BYTES_COLS)),
            }
        )
    return rows


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


# ---------------------------------------------------------------------------
# JSON -> canonical row adapters (best-effort; see module docstring)
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


def _kernel_rows_from_json(path: Path) -> list[dict]:
    root = _json_root(_load_json(path))
    dispatches = _json_records(root, "kernel_dispatch")
    names = _json_string_lookup(
        root, "kernel_symbol_info", "kernel_id", ("formatted_kernel_name", "kernel_name", "name")
    )
    rows = []
    for d in dispatches:
        kid = d.get("kernel_id")
        name = d.get("name") or names.get(kid) or f"kernel_{kid}"
        start = _numf(d.get("start_timestamp"))
        end = _numf(d.get("end_timestamp"))
        rows.append(
            {
                "name": str(name).strip(),
                "start_ns": start,
                "end_ns": end,
                "dur_ns": end - start if end > start else 0.0,
                "agent": _json_agent_id(d),
                "queue": str(d.get("queue_id", "0")),
                "corr": str(d.get("correlation_id", "")),
                "pid": str(d.get("pid", "")),
            }
        )
    return rows


def _api_rows_from_json(path: Path) -> list[dict]:
    root = _json_root(_load_json(path))
    calls = _json_records(root, "hip_api")
    rows = []
    for d in calls:
        start = _numf(d.get("start_timestamp"))
        end = _numf(d.get("end_timestamp"))
        name = str(d.get("name") or d.get("function") or "?")
        rows.append(
            {
                "name": name,
                "start_ns": start,
                "end_ns": end,
                "dur_ns": end - start if end > start else 0.0,
                "agent": "0",
                "queue": "0",
                "corr": str(d.get("correlation_id", "")),
                "pid": str(d.get("pid", "")),
            }
        )
    return rows


def _memcpy_rows_from_json(path: Path) -> list[dict]:
    root = _json_root(_load_json(path))
    copies = _json_records(root, "memory_copy")
    rows = []
    for d in copies:
        start = _numf(d.get("start_timestamp"))
        end = _numf(d.get("end_timestamp"))
        direction = str(d.get("direction") or d.get("copy_kind") or "")
        rows.append(
            {
                "direction": _normalize_direction(direction),
                "start_ns": start,
                "end_ns": end,
                "dur_ns": end - start if end > start else 0.0,
                "bytes": _numf(d.get("bytes") or d.get("size")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Multi-source loaders (CSV trace > CSV stats > JSON; concatenates PIDs)
# ---------------------------------------------------------------------------


def _load_kernel_events(disc: DiscoveredReport) -> list[dict]:
    events = [row for f in disc.kernel_trace for row in _kernel_rows_from_csv(f)]
    if events:
        return events
    return [row for f in disc.json_files for row in _kernel_rows_from_json(f)]


def _load_api_events(disc: DiscoveredReport) -> list[dict]:
    events = [row for f in disc.hip_api_trace for row in _api_rows_from_csv(f)]
    if events:
        return events
    return [row for f in disc.json_files for row in _api_rows_from_json(f)]


def _load_memcpy_events(disc: DiscoveredReport) -> list[dict]:
    events = [row for f in disc.memory_copy_trace for row in _memcpy_rows_from_csv(f)]
    if events:
        return events
    return [row for f in disc.json_files for row in _memcpy_rows_from_json(f)]


def _aggregate_kernels(events: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for e in events:
        entry = agg.setdefault(
            e["name"], {"calls": 0, "total_ns": 0.0, "min_ns": float("inf"), "max_ns": 0.0}
        )
        entry["calls"] += 1
        entry["total_ns"] += e["dur_ns"]
        entry["min_ns"] = min(entry["min_ns"], e["dur_ns"])
        entry["max_ns"] = max(entry["max_ns"], e["dur_ns"])
    return agg


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


def _load_kernels(disc: DiscoveredReport) -> tuple[dict[str, dict], str]:
    """Return (per-kernel aggregate, source label): 'trace', 'json', 'stats', or ''."""
    events = _load_kernel_events(disc)
    if events:
        return _aggregate_kernels(events), ("trace" if disc.kernel_trace else "json")
    stats_rows = [row for f in disc.kernel_stats for row in _stats_rows_from_csv(f)]
    if stats_rows:
        return _aggregate_stats(stats_rows), "stats"
    return {}, ""


def _load_api_stats(disc: DiscoveredReport) -> dict[str, dict]:
    rows = [row for f in disc.hip_api_stats for row in _stats_rows_from_csv(f)]
    return _aggregate_stats(rows)


def _load_agents(disc: DiscoveredReport) -> list[dict]:
    seen: dict[str, dict] = {}
    for f in disc.agent_info:
        for a in _agent_rows_from_csv(f):
            seen.setdefault(a["agent"], a)
    return [seen[k] for k in sorted(seen)]


def _union_duration(events: list[dict]) -> float:
    if not events:
        return 0.0
    intervals = sorted((e["start_ns"], e["end_ns"]) for e in events)
    merged: list[list[float]] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return sum(e - s for s, e in merged)


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
    merged: list[list[float]] = []
    for s, e, _n in evs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
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
    events = _load_kernel_events(disc)
    if len(events) < 2:  # noqa: PLR2004
        if disc.kernel_stats and not disc.kernel_trace:
            print(  # noqa: T201
                "(idle-gap analysis needs per-event timestamps from *_kernel_trace.csv; "
                "only aggregate *_kernel_stats.csv was found.)"
            )
        else:
            print("(fewer than 2 kernel events with timestamps found.)")  # noqa: T201
        return

    by_key: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    for e in events:
        by_key[(e["agent"], e["queue"])].append((e["start_ns"], e["end_ns"], e["name"]))

    gaps: list[tuple[tuple[str, str], str, str, float]] = []
    total_busy = 0.0
    for key, evs in by_key.items():
        busy_ns, key_gaps = _gaps_for_key(key, evs)
        total_busy += busy_ns
        gaps.extend(key_gaps)

    gaps.sort(key=lambda x: -x[3])
    total_idle = sum(g[3] for g in gaps)
    window_ns = max(e["end_ns"] for e in events) - min(e["start_ns"] for e in events)

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

    return parser


_COMMANDS = {
    "files": cmd_files,
    "kernels": cmd_kernels,
    "families": cmd_families,
    "idle_gaps": cmd_idle_gaps,
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
