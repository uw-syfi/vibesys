#!/usr/bin/env python3
"""rocprofv3 PMC counter-set catalogue, report aggregation, and bottleneck triage.

Hardware performance counters (PMC) on AMD Instinct GPUs are collected with
``rocprofv3 --pmc <names...>``. Packing many counters into one job forces a
multi-pass collection, which has been observed to hang the GPU on gfx942.
This toolkit keeps every named counter set at or under 4 counters and drives
one ``rocprofv3`` process (one hardware pass, one output directory) per set,
so the agent never hand-builds an oversized ad-hoc counter list.

Usage:
    python counters.py list-sets [--arch gfx90a]
    python counters.py plan --arch gfx90a --sets mfma,l2,hbm [--kernel REGEX]
    python counters.py report <dir> [<dir> ...] [--kernel REGEX] [--top 15]
    python counters.py triage <dir> [<dir> ...] --arch gfx90a [--kernel REGEX]

``report``/``triage`` read every ``*counter_collection*.csv`` (or ``.json``)
file under the given directories -- each directory is normally one
``plan``-generated pass -- and merge the counters per kernel name. A
``*kernel_trace*.csv`` file found alongside them (from a separate
``rocprofv3 --kernel-trace`` capture) supplies per-kernel wall-clock duration,
which several derived metrics (achieved HBM bandwidth) need and PMC alone
does not provide.

Counter CSV schema (documented rocprofv3 ``*_counter_collection.csv``):
    Correlation_Id, Dispatch_Id, Agent_Id, Queue_Id, Process_Id, Thread_Id,
    Grid_Size, Kernel_Id, Kernel_Name, Workgroup_Size, LDS_Block_Size,
    Scratch_Size, VGPR_Count, SGPR_Count, Counter_Name, Counter_Value

Not every counter name below has been confirmed against a real capture on
every architecture (see ``verified=False`` entries in ``COUNTER_SETS``); this
toolkit was built against rocprofv3's documented CSV/JSON shapes while real
MI210 samples were still being collected. ``report``/``triage`` degrade
gracefully (print "n/a" with the counters they looked for) when a name does
not match what a capture actually produced.
"""  # noqa: EXE001  # tracked: #288

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WAVEFRONT_SIZE = 64
SIMDS_PER_CU = 4
MAX_WAVES_PER_SIMD = 8
SGPR_BUDGET_PER_SIMD = 800
SGPR_ALLOC_GRANULARITY = 16
VGPR_BUDGET_PER_SIMD = 512
VGPR_ALLOC_GRANULARITY = 8

# ---------------------------------------------------------------------------
# Architecture identification
# ---------------------------------------------------------------------------

ARCH_ALIASES: dict[str, str] = {
    "mi210": "gfx90a",
    "mi250": "gfx90a",
    "mi250x": "gfx90a",
    "mi300a": "gfx942",
    "mi300x": "gfx942",
    "mi325x": "gfx942",
    "mi350x": "gfx950",
    "mi355x": "gfx950",
}


def normalize_arch(name: str) -> str:
    """Map a SKU name or gfx id to a canonical ``gfxNNN`` family id."""
    key = name.strip().lower()
    if key in ARCH_ALIASES:
        return ARCH_ALIASES[key]
    if key.startswith("gfx"):
        return key
    known = sorted(set(ARCH_ALIASES.values()))
    raise ValueError(f"unknown architecture {name!r}; known families: {', '.join(known)}")  # noqa: TRY003  # tracked: #288


def _normalize_arch_or_exit(name: str) -> str:
    """CLI-facing wrapper: turn an unknown-architecture ``ValueError`` into a clean exit."""
    try:
        return normalize_arch(name)
    except ValueError as exc:
        sys.exit(str(exc))


@dataclass(frozen=True)
class PeakSpec:
    """Per-architecture peak constants for roofline placement.

    These are datasheet peaks, not achievable ceilings: tuned GEMM libraries
    typically land at ~45-55% of the dense matrix peak, and HBM bandwidth
    tests rarely clear ~80-85% of the rated number. Use a measured baseline
    (best library kernel, empirical bandwidth test) as the real bar; treat
    the numbers here only as the outer envelope.
    """

    label: str
    compute_units: int
    dense_bf16_fp16_tflops: float
    hbm_tb_s: float
    hbm_gb: int
    source: str = "public spec sheet"

    @property
    def ridge_flop_per_byte(self) -> float:
        """Where the sloped BW roof meets the flat compute roof (bf16/fp16)."""
        return self.dense_bf16_fp16_tflops * 1e12 / (self.hbm_tb_s * 1e12)


PEAK_SPECS: dict[str, PeakSpec] = {
    "gfx90a": PeakSpec("MI210", 104, 181.0, 1.6, 64),
    "gfx942": PeakSpec("MI300X/MI300A/MI325X (gfx942, see note)", 304, 1307.4, 5.3, 192),
    "gfx950": PeakSpec("MI355X", 256, 2500.0, 8.0, 288),
}

PEAK_ARCH_NOTE = (
    "gfx942 covers three SKUs with different CU counts and memory sizes "
    "(MI300X 304 CU/192GB/5.3TB/s, MI300A 228 CU/128GB/~5.3TB/s, MI325X "
    "304 CU/256GB/6.0TB/s @ ~1307.4 dense bf16/fp16 TFLOP/s each); the table "
    "uses MI300X's numbers as the representative gfx942 entry. Pass the "
    "exact SKU's HBM figures manually when triaging MI300A or MI325X."
)


# ---------------------------------------------------------------------------
# Counter-set catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterSet:
    """A named, <=4-counter rocprofv3 ``--pmc`` group for one architecture."""

    counters: tuple[str, ...]
    verified: bool
    note: str = ""

    def __post_init__(self) -> None:  # noqa: D105  # tracked: #288
        if not 1 <= len(self.counters) <= 4:  # noqa: PLR2004  # tracked: #288
            msg = f"counter set must have 1-4 counters, got {len(self.counters)}"
            raise ValueError(msg)


_GFX942_HBM = CounterSet(
    (
        "TCC_EA0_RDREQ_sum",
        "TCC_EA0_RDREQ_32B_sum",
        "TCC_EA0_RDREQ_DRAM_sum",
        "TCP_TCC_READ_REQ_sum",
    ),
    verified=True,
    note="Confirmed working rocprofv3 config on gfx942. EA0 is one EA/channel instance.",
)

COUNTER_SETS: dict[str, dict[str, CounterSet]] = {
    "gfx90a": {
        "occupancy": CounterSet(
            ("SQ_WAVES", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
            note="waves launched vs. GPU-busy / total cycles.",
        ),
        "mfma": CounterSet(
            ("SQ_INSTS_VALU_MFMA_MOPS_BF16", "SQ_INSTS_VALU_MFMA_MOPS_F16", "GRBM_COUNT"),
            verified=False,
            note="CDNA2 MFMA MOPS counter names inferred from CDNA3 pattern; confirm with --list-avail.",
        ),
        "valu": CounterSet(
            ("SQ_INSTS_VALU", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
        ),
        "l2": CounterSet(
            ("TCC_HIT_sum", "TCC_MISS_sum", "TCP_TCC_READ_REQ_sum"),
            verified=True,
            note="TCC block names match the verified gfx942 config; TCC is a common gfx9 IP block.",
        ),
        "hbm": CounterSet(
            (
                "TCC_EA_RDREQ_sum",
                "TCC_EA_RDREQ_32B_sum",
                "TCC_EA_RDREQ_DRAM_sum",
                "TCP_TCC_READ_REQ_sum",
            ),
            verified=False,
            note="gfx90a is single-die (no XCD split); EA channel suffix dropped vs. gfx942's EA0. Unconfirmed.",
        ),
        "lds": CounterSet(
            ("SQ_INSTS_LDS", "SQ_LDS_BANK_CONFLICT", "GRBM_COUNT"),
            verified=False,
        ),
    },
    "gfx942": {
        "occupancy": CounterSet(
            ("SQ_WAVES", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
        ),
        "mfma": CounterSet(
            (
                "SQ_INSTS_VALU_MFMA_MOPS_BF16",
                "SQ_INSTS_VALU_MFMA_MOPS_F16",
                "SQ_INSTS_VALU_MFMA_MOPS_FP8",
                "GRBM_COUNT",
            ),
            verified=False,
            note="MOPS-per-dtype counter names are the common AMD convention; not confirmed on-device here.",
        ),
        "valu": CounterSet(
            ("SQ_INSTS_VALU", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
        ),
        "l2": CounterSet(
            ("TCC_HIT_sum", "TCC_MISS_sum", "TCP_TCC_READ_REQ_sum"),
            verified=True,
            note="Confirmed working rocprofv3 config on gfx942.",
        ),
        "hbm": _GFX942_HBM,
        "lds": CounterSet(
            ("SQ_INSTS_LDS", "SQ_LDS_BANK_CONFLICT", "GRBM_COUNT"),
            verified=False,
        ),
    },
    "gfx950": {
        "occupancy": CounterSet(
            ("SQ_WAVES", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
        ),
        "mfma": CounterSet(
            (
                "SQ_INSTS_VALU_MFMA_MOPS_BF16",
                "SQ_INSTS_VALU_MFMA_MOPS_FP8",
                "SQ_INSTS_VALU_MFMA_MOPS_FP6",
                "GRBM_COUNT",
            ),
            verified=False,
            note="CDNA4 adds FP6/FP4 MFMA; the FP6 MOPS counter name is a guess, confirm with --list-avail.",
        ),
        "valu": CounterSet(
            ("SQ_INSTS_VALU", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=False,
        ),
        "l2": CounterSet(
            ("TCC_HIT_sum", "TCC_MISS_sum", "TCP_TCC_READ_REQ_sum"),
            verified=False,
            note="Assumed unchanged from gfx942's TCC block; not confirmed on gfx950.",
        ),
        "hbm": CounterSet(
            _GFX942_HBM.counters,
            verified=False,
            note="Assumed unchanged EA-channel convention from gfx942; not confirmed on gfx950.",
        ),
        "lds": CounterSet(
            ("SQ_INSTS_LDS", "SQ_LDS_BANK_CONFLICT", "GRBM_COUNT"),
            verified=False,
            note="gfx950 LDS is 160KB/CU vs. 64KB/CU on gfx90a/gfx942 (see occupancy note in `report`).",
        ),
    },
}


CANDIDATES: dict[str, tuple[str, ...]] = {
    "l2_hit": ("TCC_HIT_sum", "TCC_HIT[0]_sum"),
    "l2_miss": ("TCC_MISS_sum", "TCC_MISS[0]_sum"),
    "hbm_rdreq": ("TCC_EA0_RDREQ_sum", "TCC_EA_RDREQ_sum"),
    "hbm_rdreq_32b": ("TCC_EA0_RDREQ_32B_sum", "TCC_EA_RDREQ_32B_sum"),
    "hbm_wrreq": ("TCC_EA0_WRREQ_sum", "TCC_EA_WRREQ_sum"),
    "hbm_wrreq_32b": ("TCC_EA0_WRREQ_32B_sum", "TCC_EA_WRREQ_32B_sum"),
    "busy_cycles": ("GRBM_GUI_ACTIVE",),
    "total_cycles": ("GRBM_COUNT",),
    "waves": ("SQ_WAVES",),
    "valu_insts": ("SQ_INSTS_VALU",),
    "mfma_insts_bf16": ("SQ_INSTS_VALU_MFMA_MOPS_BF16",),
    "mfma_insts_f16": ("SQ_INSTS_VALU_MFMA_MOPS_F16",),
    "mfma_insts_fp8": ("SQ_INSTS_VALU_MFMA_MOPS_FP8",),
    "mfma_insts_fp6": ("SQ_INSTS_VALU_MFMA_MOPS_FP6",),
    "lds_insts": ("SQ_INSTS_LDS",),
    "lds_bank_conflict": ("SQ_LDS_BANK_CONFLICT",),
}

MFMA_CANDIDATE_KEYS = ("mfma_insts_bf16", "mfma_insts_f16", "mfma_insts_fp8", "mfma_insts_fp6")


def _lookup(counters: dict[str, float], key: str) -> float | None:
    for name in CANDIDATES[key]:
        if name in counters:
            return counters[name]
    return None


@dataclass(frozen=True)
class CounterRow:
    """One (kernel dispatch, counter) sample, normalized from a CSV or JSON row."""

    kernel_name: str
    dispatch_id: str
    grid_size: int
    workgroup_size: int
    lds_block_size: int
    scratch_size: int
    vgpr_count: int
    sgpr_count: int
    counter_name: str
    counter_value: float


def _to_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _row_from_mapping(row: dict[str, Any]) -> CounterRow | None:
    name = row.get("Kernel_Name") or row.get("kernel_name")
    counter_name = row.get("Counter_Name") or row.get("counter_name")
    counter_value = row.get("Counter_Value", row.get("counter_value"))
    if not name or not counter_name or counter_value in (None, ""):
        return None
    return CounterRow(
        kernel_name=str(name),
        dispatch_id=str(row.get("Dispatch_Id", row.get("dispatch_id", ""))),
        grid_size=_to_int(row.get("Grid_Size", row.get("grid_size"))),
        workgroup_size=_to_int(row.get("Workgroup_Size", row.get("workgroup_size"))),
        lds_block_size=_to_int(row.get("LDS_Block_Size", row.get("lds_block_size"))),
        scratch_size=_to_int(row.get("Scratch_Size", row.get("scratch_size"))),
        vgpr_count=_to_int(row.get("VGPR_Count", row.get("vgpr_count"))),
        sgpr_count=_to_int(row.get("SGPR_Count", row.get("sgpr_count"))),
        counter_name=str(counter_name),
        counter_value=_to_float(counter_value),
    )


def _iter_csv_rows(path: Path) -> list[CounterRow]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = [_row_from_mapping(r) for r in csv.DictReader(f)]
    return [r for r in rows if r is not None]


def _walk_json_records(node: object) -> list[dict[str, Any]]:
    """Recursively find dicts that look like a counter row.

    Tolerates rocprofv3's nested ``{"rocprofiler-sdk-tool": {"counter_collection":
    [...]}}`` JSON shape as well as a flat list of CSV-equivalent row dicts.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        has_name = any(k in node for k in ("Counter_Name", "counter_name"))
        has_value = any(k in node for k in ("Counter_Value", "counter_value"))
        if has_name and has_value:
            found.append(node)
        else:
            for value in node.values():
                found.extend(_walk_json_records(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_json_records(item))
    return found


def _iter_json_rows(path: Path) -> list[CounterRow]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = [_row_from_mapping(r) for r in _walk_json_records(data)]
    return [r for r in rows if r is not None]


def _load_counter_rows(files: list[Path]) -> list[CounterRow]:
    rows: list[CounterRow] = []
    for path in files:
        try:
            if path.suffix == ".json":
                rows.extend(_iter_json_rows(path))
            else:
                rows.extend(_iter_csv_rows(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: could not parse {path}: {exc}", file=sys.stderr)  # noqa: T201  # tracked: #288
    return rows


def _discover(dirs: list[str], substring: str, suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for raw in dirs:
        base = Path(raw)
        if not base.exists():
            print(f"warning: directory not found: {raw}", file=sys.stderr)  # noqa: T201  # tracked: #288
            continue
        found.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file() and substring in path.name and path.suffix in suffixes
        )
    return found


def _load_kernel_trace_durations(dirs: list[str]) -> dict[str, float]:
    """Sum kernel wall-clock duration (ns) per Kernel_Name from any
    ``*kernel_trace*.csv`` files under the given directories.
    """  # noqa: D205  # tracked: #288
    totals: dict[str, float] = defaultdict(float)
    for path in _discover(dirs, "kernel_trace", (".csv",)):
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("Kernel_Name")
                start = row.get("Start_Timestamp") or row.get("start")
                end = row.get("End_Timestamp") or row.get("end")
                if not name or start is None or end is None:
                    continue
                duration = _to_float(end) - _to_float(start)
                if duration > 0:
                    totals[str(name)] += duration
    return dict(totals)


@dataclass
class KernelAgg:
    """Per-kernel counters merged across every PMC pass, plus peak resource usage."""

    name: str
    dispatch_ids: set[str] = field(default_factory=set)
    counters: dict[str, float] = field(default_factory=dict)
    grid_size: int = 0
    workgroup_size: int = 0
    lds_block_size: int = 0
    scratch_size: int = 0
    vgpr_count: int = 0
    sgpr_count: int = 0

    @property
    def dispatch_count(self) -> int:
        """Number of distinct dispatches merged into this aggregate (at least 1)."""
        return len(self.dispatch_ids) or 1


def _aggregate_by_kernel(rows: list[CounterRow]) -> dict[str, KernelAgg]:
    aggs: dict[str, KernelAgg] = {}
    for row in rows:
        agg = aggs.setdefault(row.kernel_name, KernelAgg(name=row.kernel_name))
        if row.dispatch_id:
            agg.dispatch_ids.add(row.dispatch_id)
        agg.counters[row.counter_name] = agg.counters.get(row.counter_name, 0.0) + row.counter_value
        # Resource usage is constant per dispatch; keep the largest seen so a
        # kernel launched with varying grid/workgroup shape reports its peak.
        agg.grid_size = max(agg.grid_size, row.grid_size)
        agg.workgroup_size = max(agg.workgroup_size, row.workgroup_size)
        agg.lds_block_size = max(agg.lds_block_size, row.lds_block_size)
        agg.scratch_size = max(agg.scratch_size, row.scratch_size)
        agg.vgpr_count = max(agg.vgpr_count, row.vgpr_count)
        agg.sgpr_count = max(agg.sgpr_count, row.sgpr_count)
    return aggs


def _filter_kernels(aggs: dict[str, KernelAgg], pattern: str | None) -> list[KernelAgg]:
    values = list(aggs.values())
    if pattern:
        rx = re.compile(pattern)
        values = [a for a in values if rx.search(a.name)]
    return sorted(values, key=lambda a: (-a.dispatch_count, a.name))


@dataclass(frozen=True)
class Occupancy:
    """Resource-bound max occupancy for one kernel (waves/SIMD and waves/CU)."""

    vgpr_limit: int
    lds_limit: int
    sgpr_limit: int
    waves_per_simd: int
    waves_per_cu: int
    bound_by: str
    lds_total_bytes: int


def _round_up(value: int, granularity: int) -> int:
    if value <= 0:
        return 0
    return ((value + granularity - 1) // granularity) * granularity


def resource_occupancy(
    *, vgpr_count: int, sgpr_count: int, lds_block_size: int, workgroup_size: int, arch: str
) -> Occupancy:
    """Theoretical max occupancy bounded by VGPR/LDS/SGPR allocation.

    This is the standard AMD CDNA occupancy model: a combined 512-VGPR pool
    per SIMD (arch+accum unified since CDNA2/gfx90a), 800 SGPRs per SIMD, and
    an LDS budget of 64KB/CU on gfx90a and gfx942 vs. 160KB/CU on gfx950. It
    is a resource ceiling, not a measured wave count -- a kernel can occupy
    fewer waves than this bound for other reasons (barriers, scheduling).
    """
    family = normalize_arch(arch)
    lds_total = 163840 if family == "gfx950" else 65536

    vgpr_alloc = _round_up(vgpr_count, VGPR_ALLOC_GRANULARITY)
    vgpr_limit = (
        min(VGPR_BUDGET_PER_SIMD // vgpr_alloc, MAX_WAVES_PER_SIMD)
        if vgpr_alloc
        else MAX_WAVES_PER_SIMD
    )

    if lds_block_size > 0 and workgroup_size > 0:
        waves_per_wg = max(1, (workgroup_size + WAVEFRONT_SIZE - 1) // WAVEFRONT_SIZE)
        wg_per_cu = lds_total // lds_block_size
        lds_limit = min(max(1, (wg_per_cu * waves_per_wg) // SIMDS_PER_CU), MAX_WAVES_PER_SIMD)
    else:
        lds_limit = MAX_WAVES_PER_SIMD

    sgpr_alloc = _round_up(sgpr_count, SGPR_ALLOC_GRANULARITY)
    sgpr_limit = (
        min(SGPR_BUDGET_PER_SIMD // sgpr_alloc, MAX_WAVES_PER_SIMD)
        if sgpr_alloc
        else MAX_WAVES_PER_SIMD
    )

    limits = {"VGPR": vgpr_limit, "LDS": lds_limit, "SGPR": sgpr_limit}
    bound_by = min(limits, key=lambda k: limits[k])
    waves_per_simd = limits[bound_by]
    return Occupancy(
        vgpr_limit=vgpr_limit,
        lds_limit=lds_limit,
        sgpr_limit=sgpr_limit,
        waves_per_simd=waves_per_simd,
        waves_per_cu=waves_per_simd * SIMDS_PER_CU,
        bound_by=bound_by,
        lds_total_bytes=lds_total,
    )


@dataclass
class DerivedMetrics:
    """Ratios and rates computed from whichever counters a kernel's merged passes contain."""

    l2_hit_rate_pct: float | None = None
    hbm_bytes: float | None = None
    achieved_bw_gb_s: float | None = None
    gpu_busy_pct: float | None = None
    mfma_issue_rate: float | None = None
    lds_bank_conflict_rate_pct: float | None = None
    duration_ns: float | None = None


def _hbm_bytes(counters: dict[str, float]) -> float | None:
    total = 0.0
    found_any = False
    for req_key, partial_key in (("hbm_rdreq", "hbm_rdreq_32b"), ("hbm_wrreq", "hbm_wrreq_32b")):
        req = _lookup(counters, req_key)
        if req is None:
            continue
        found_any = True
        partial = _lookup(counters, partial_key) or 0.0
        full = req - partial
        total += full * 64 + partial * 32
    return total if found_any else None


def derive_metrics(agg: KernelAgg, duration_ns: float | None) -> DerivedMetrics:
    """Compute every derived metric whose inputs are present in ``agg.counters``."""
    counters = agg.counters
    metrics = DerivedMetrics(duration_ns=duration_ns)

    hit, miss = _lookup(counters, "l2_hit"), _lookup(counters, "l2_miss")
    if hit is not None and miss is not None and (hit + miss) > 0:
        metrics.l2_hit_rate_pct = 100.0 * hit / (hit + miss)

    metrics.hbm_bytes = _hbm_bytes(counters)
    if metrics.hbm_bytes is not None and duration_ns and duration_ns > 0:
        metrics.achieved_bw_gb_s = metrics.hbm_bytes / (duration_ns / 1e9) / 1e9

    busy, total = _lookup(counters, "busy_cycles"), _lookup(counters, "total_cycles")
    if busy is not None and total is not None and total > 0:
        metrics.gpu_busy_pct = 100.0 * busy / total

    mfma_total = sum(v for k in MFMA_CANDIDATE_KEYS if (v := _lookup(counters, k)) is not None)
    if mfma_total > 0 and total is not None and total > 0:
        metrics.mfma_issue_rate = mfma_total / total

    lds_insts, bank_conflicts = (
        _lookup(counters, "lds_insts"),
        _lookup(counters, "lds_bank_conflict"),
    )
    if lds_insts is not None and bank_conflicts is not None and lds_insts > 0:
        metrics.lds_bank_conflict_rate_pct = 100.0 * bank_conflicts / lds_insts

    return metrics


def cmd_list_sets(ns: argparse.Namespace) -> None:
    """Print the counter-set catalogue for one architecture, or all of them."""
    families = [_normalize_arch_or_exit(ns.arch)] if ns.arch else sorted(COUNTER_SETS)
    for family in families:
        if family not in PEAK_SPECS:
            known = ", ".join(sorted(PEAK_SPECS))
            sys.exit(f"no counter-set catalogue for {family!r}; known families: {known}")
        spec = PEAK_SPECS[family]
        print(f"\n{family} ({spec.label})")  # noqa: T201  # tracked: #288
        for set_name, cset in sorted(COUNTER_SETS[family].items()):
            flag = "verified" if cset.verified else "UNVERIFIED"
            print(f"  {set_name:<12} [{flag}] {', '.join(cset.counters)}")  # noqa: T201  # tracked: #288
            if cset.note:
                print(f"               {cset.note}")  # noqa: T201  # tracked: #288


def _plan_command(*, out_dir: str, set_name: str, cset: CounterSet, ns: argparse.Namespace) -> str:
    command_tokens = [t for t in ns.command if t != "--"]
    command = shlex.join(command_tokens) if command_tokens else "<your_command_and_args>"
    lines = [f"rocprofv3 --pmc {' '.join(cset.counters)}"]
    if ns.kernel:
        lines.append(f"    --kernel-include-regex {shlex.quote(ns.kernel)}")
    lines.append(f"    -d {out_dir} -o {set_name}")
    lines.append(f"    -- {command}")
    return " \\\n".join(lines)


def _plan_one_set(*, arch: str, set_name: str, cset: CounterSet, ns: argparse.Namespace) -> None:
    out_dir = f"{ns.out_dir}/{arch}/{set_name}"
    flag = "verified" if cset.verified else "UNVERIFIED -- confirm with `rocprofv3 --list-avail`"
    print(f"\n# {set_name} [{flag}]")  # noqa: T201  # tracked: #288
    if cset.note:
        print(f"# {cset.note}")  # noqa: T201  # tracked: #288
    print(_plan_command(out_dir=out_dir, set_name=set_name, cset=cset, ns=ns))  # noqa: T201  # tracked: #288


def cmd_plan(ns: argparse.Namespace) -> None:
    """Print one rocprofv3 --pmc command line per requested counter set.

    Each set gets its own process invocation and its own output directory --
    never combine sets into one --pmc call, and never re-use an output
    directory across passes.
    """  # tracked: #288
    arch = _normalize_arch_or_exit(ns.arch)
    if arch not in COUNTER_SETS:
        known = ", ".join(sorted(COUNTER_SETS))
        sys.exit(f"no counter-set catalogue for {arch!r}; known families: {known}")
    catalogue = COUNTER_SETS[arch]
    requested = [s.strip() for s in ns.sets.split(",") if s.strip()]
    unknown = [s for s in requested if s not in catalogue]
    if unknown:
        known = ", ".join(sorted(catalogue))
        sys.exit(f"unknown counter set(s) {unknown} for {arch}; known sets: {known}")

    spec = PEAK_SPECS[arch]
    header = f"# {len(requested)} pass(es) for {arch} ({spec.label}); one process per pass, own output dir each."
    print(header)  # noqa: T201  # tracked: #288
    for set_name in requested:
        _plan_one_set(arch=arch, set_name=set_name, cset=catalogue[set_name], ns=ns)
    print(  # noqa: T201  # tracked: #288
        "\n# Merge and analyze with:\n"
        f"#   python counters.py report {ns.out_dir}/{arch}/<set1> {ns.out_dir}/{arch}/<set2> ..."
    )


def _print_kernel_resources(agg: KernelAgg, occ: Occupancy) -> None:
    print(  # noqa: T201  # tracked: #288
        f"  grid={agg.grid_size} wg={agg.workgroup_size} "
        f"vgpr={agg.vgpr_count} sgpr={agg.sgpr_count} "
        f"lds={agg.lds_block_size}B scratch={agg.scratch_size}B"
    )
    print(  # noqa: T201  # tracked: #288
        f"  occupancy: {occ.waves_per_simd}/{MAX_WAVES_PER_SIMD} waves/SIMD "
        f"({occ.waves_per_cu} waves/CU), bound by {occ.bound_by} "
        f"(vgpr<={occ.vgpr_limit} lds<={occ.lds_limit} sgpr<={occ.sgpr_limit})"
    )


def _fmt(value: float | None, suffix: str = "", precision: int = 2) -> str:
    return f"{value:.{precision}f}{suffix}" if value is not None else "n/a"


def _print_derived(metrics: DerivedMetrics) -> None:
    print(  # noqa: T201  # tracked: #288
        f"  L2 hit rate: {_fmt(metrics.l2_hit_rate_pct, '%', 1)}   "
        f"GPU busy: {_fmt(metrics.gpu_busy_pct, '%', 1)}   "
        f"MFMA issue rate: {_fmt(metrics.mfma_issue_rate, ' insts/cycle', 4)}"
    )
    print(  # noqa: T201  # tracked: #288
        f"  HBM bytes: {_fmt(metrics.hbm_bytes, ' B', 0)}   "
        f"achieved BW: {_fmt(metrics.achieved_bw_gb_s, ' GB/s', 1)}"
        + (
            " (no kernel_trace duration found)"
            if metrics.hbm_bytes is not None and metrics.duration_ns is None
            else ""
        )
    )
    print(f"  LDS bank-conflict rate: {_fmt(metrics.lds_bank_conflict_rate_pct, '%', 1)}")  # noqa: T201  # tracked: #288


def _report_arch_for(ns: argparse.Namespace) -> str | None:
    arch = getattr(ns, "arch", None)
    return _normalize_arch_or_exit(arch) if arch else None


def cmd_report(ns: argparse.Namespace) -> None:
    """Merge PMC passes per kernel and print resource usage + derived metrics."""
    counter_files = _discover(ns.dirs, "counter_collection", (".csv", ".json"))
    if not counter_files:
        print("(no *counter_collection*.csv/.json files found under the given directories)")  # noqa: T201  # tracked: #288
        return
    rows = _load_counter_rows(counter_files)
    if not rows:
        print("(counter files found but no usable rows parsed)")  # noqa: T201  # tracked: #288
        return
    durations = _load_kernel_trace_durations(ns.dirs)
    aggs = _aggregate_by_kernel(rows)
    kernels = _filter_kernels(aggs, ns.kernel)
    arch = _report_arch_for(ns)

    print(f"Merged {len(counter_files)} counter file(s), {len(kernels)} kernel(s) matched.")  # noqa: T201  # tracked: #288
    if durations:
        print(f"Kernel-trace duration available for {len(durations)} kernel name(s).")  # noqa: T201  # tracked: #288

    for agg in kernels[: ns.top]:
        duration = durations.get(agg.name)
        occ = resource_occupancy(
            vgpr_count=agg.vgpr_count,
            sgpr_count=agg.sgpr_count,
            lds_block_size=agg.lds_block_size,
            workgroup_size=agg.workgroup_size,
            arch=arch or "gfx942",
        )
        metrics = derive_metrics(agg, duration)
        print(f"\n{agg.name}  ({agg.dispatch_count} dispatch(es))")  # noqa: T201  # tracked: #288
        _print_kernel_resources(agg, occ)
        _print_derived(metrics)

    remainder = kernels[ns.top :]
    if remainder:
        print(f"\n( +{len(remainder)} more kernel(s) not shown; raise --top )")  # noqa: T201  # tracked: #288


def _add_dirs_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dirs", nargs="+", help="One or more PMC pass output directories")
    parser.add_argument("--kernel", default=None, help="Regex filter on Kernel_Name")
    parser.add_argument("--top", type=int, default=15)


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="counters",
        description="rocprofv3 PMC counter-set catalogue and report aggregation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_sets = sub.add_parser("list-sets", help="print the counter-set catalogue")
    list_sets.add_argument(
        "--arch", default=None, help="e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x"
    )
    list_sets.set_defaults(fn=cmd_list_sets)

    plan = sub.add_parser("plan", help="print rocprofv3 --pmc command lines, one pass per set")
    plan.add_argument("--arch", required=True)
    plan.add_argument("--sets", required=True, help="comma-separated set names, e.g. mfma,l2,hbm")
    plan.add_argument("--kernel", default=None, help="rocprofv3 --kernel-include-regex value")
    plan.add_argument("--out-dir", default="rocprof_pmc")
    plan.add_argument("command", nargs=argparse.REMAINDER, help="the program to profile, after --")
    plan.set_defaults(fn=cmd_plan)

    report = sub.add_parser("report", help="merge PMC passes per kernel and print derived metrics")
    _add_dirs_arg(report)
    report.add_argument(
        "--arch", default=None, help="for the occupancy LDS-size model; default gfx942"
    )
    report.set_defaults(fn=cmd_report)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
