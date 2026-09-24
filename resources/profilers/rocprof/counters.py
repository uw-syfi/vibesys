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

Every gfx90a entry in ``COUNTER_SETS`` has been checked against a real MI210
(gfx90a, ROCm 6.4.1) ``rocprofv3 --list-avail`` dump; ``verified=True`` means
every counter name in that set exists for gfx90a, with the note saying
whether the exact combination was also captured together on real hardware.
gfx942/gfx950 entries not marked verified are still unconfirmed. ``report``/
``triage`` degrade gracefully (print "n/a" with the counters they looked for)
when a name does not match what a capture actually produced.

Real MI210 captures (ROCm 6.4.1) also confirmed two behaviors this toolkit
now assumes:

- ``*_counter_collection.csv`` carries ``Start_Timestamp``/``End_Timestamp``
  on every row (one pair per dispatch, repeated across that dispatch's
  counter rows). Per-kernel duration -- needed for achieved-bandwidth --
  is derived straight from these columns; a separate ``--kernel-trace``
  capture is optional, not required, and only overrides the PMC-derived
  duration where present.
- ``rocprofv3 -d <dir> ...`` always nests output under
  ``<dir>/pmc_1/<hostname>/<pid>_*``, never directly under ``<dir>``, and
  the ``pmc_1`` segment names "this run's first PMC pass", not the counter
  set requested. ``report``/``triage`` discover files recursively so this
  doesn't matter for parsing, but don't expect flat ``<dir>/<file>`` paths.

``--output-format json`` was also captured on real hardware, but rocprofv3's
JSON output is a normalized/relational schema (separate ``counters``,
``kernel_symbols``, ``buffer_records`` tables joined by id) -- it is not the
flat per-row shape this toolkit's JSON reader assumes. That reader only
handles a flat ``[{"Counter_Name": ..., "Counter_Value": ...}, ...]`` shape
(or nested lists/dicts of it) and will silently find zero rows against real
rocprofv3 JSON. Treat JSON support as unverified; CSV is the confirmed,
recommended format, and ``plan`` only asks for CSV.
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
            verified=True,
            note="All 3 names confirmed in a real MI210 --list-avail dump. SQ_WAVES + "
            "GRBM_GUI_ACTIVE were also captured together on real hardware (job1 pass1); "
            "GRBM_COUNT (same GRBM block) wasn't in that pass but is unlikely to conflict.",
        ),
        "mfma": CounterSet(
            (
                "SQ_INSTS_MFMA",
                "SQ_INSTS_VALU_MFMA_MOPS_BF16",
                "SQ_INSTS_VALU_MFMA_MOPS_F16",
                "GRBM_COUNT",
            ),
            verified=True,
            note="SQ_INSTS_MFMA (total MFMA instructions issued) is the counter a real MI210 "
            "capture used (job1 pass1) and is what report/triage prefer for the MFMA issue "
            "rate. MOPS_BF16/MOPS_F16 (per-dtype math-op counts, a different unit -- already "
            "divided by 512) and GRBM_COUNT are confirmed present in --list-avail but weren't "
            "captured in the same pass as SQ_INSTS_MFMA. SQ_VALU_MFMA_BUSY_CYCLES also exists "
            "on gfx90a as an alternative (per-SIMD MFMA-busy cycles) if instruction counts turn "
            "out too coarse; not yet captured on real hardware.",
        ),
        "valu": CounterSet(
            ("SQ_INSTS_VALU", "GRBM_GUI_ACTIVE", "GRBM_COUNT"),
            verified=True,
            note="All 3 names confirmed in --list-avail; SQ_INSTS_VALU + GRBM_GUI_ACTIVE were "
            "also captured together on real hardware (job1 pass1).",
        ),
        "l2": CounterSet(
            ("TCC_HIT_sum", "TCC_MISS_sum", "TCP_TCC_READ_REQ_sum"),
            verified=True,
            note="TCC_HIT_sum + TCC_MISS_sum captured together on real MI210 hardware (job1 "
            "pass2). TCP_TCC_READ_REQ_sum is confirmed present in --list-avail but wasn't in "
            "that pass.",
        ),
        "hbm": CounterSet(
            (
                "TCC_EA_RDREQ_sum",
                "TCC_EA_RDREQ_32B_sum",
                "TCC_EA_RDREQ_DRAM_sum",
                "TCP_TCC_READ_REQ_sum",
            ),
            verified=True,
            note="gfx90a is single-die (no XCD split): EA channel suffix is dropped vs. "
            "gfx942's EA0. All 4 names confirmed in a real MI210 --list-avail dump. "
            "TCC_EA_RDREQ_sum (paired with TCC_EA_WRREQ_sum, not this exact 4-counter combo) "
            "was captured on real hardware (job1 pass3).",
        ),
        "lds": CounterSet(
            ("SQ_INSTS_LDS", "SQ_LDS_BANK_CONFLICT", "GRBM_COUNT"),
            verified=True,
            note="All 3 names confirmed in a real MI210 --list-avail dump; not yet captured "
            "together on real hardware.",
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
    "hbm_wrreq_64b": ("TCC_EA0_WRREQ_64B_sum", "TCC_EA_WRREQ_64B_sum"),
    "busy_cycles": ("GRBM_GUI_ACTIVE",),
    "total_cycles": ("GRBM_COUNT",),
    "waves": ("SQ_WAVES",),
    "valu_insts": ("SQ_INSTS_VALU",),
    "mfma_insts_total": ("SQ_INSTS_MFMA",),
    "mfma_insts_bf16": ("SQ_INSTS_VALU_MFMA_MOPS_BF16",),
    "mfma_insts_f16": ("SQ_INSTS_VALU_MFMA_MOPS_F16",),
    "mfma_insts_fp8": ("SQ_INSTS_VALU_MFMA_MOPS_FP8",),
    "mfma_insts_fp6": ("SQ_INSTS_VALU_MFMA_MOPS_FP6",),
    "lds_insts": ("SQ_INSTS_LDS",),
    "lds_bank_conflict": ("SQ_LDS_BANK_CONFLICT",),
}

# Per-dtype MOPS_* counters are a fallback only: they count math *operations*
# (already divided by 512) rather than raw instructions, a different unit
# from SQ_INSTS_MFMA, so the two are never summed together (see
# `_mfma_instruction_count`).
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
    start_ns: float | None = None
    end_ns: float | None = None


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


def _to_optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
        start_ns=_to_optional_float(row.get("Start_Timestamp", row.get("start_timestamp"))),
        end_ns=_to_optional_float(row.get("End_Timestamp", row.get("end_timestamp"))),
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


def _duration_from_counter_rows(rows: list[CounterRow]) -> dict[str, float]:
    """Sum per-dispatch wall-clock duration (ns) straight from the PMC rows'
    own ``Start_Timestamp``/``End_Timestamp`` columns.

    Real rocprofv3 6.4.1 ``*_counter_collection.csv`` carries these on every
    row; a dedicated ``--kernel-trace`` capture is not required to get
    duration data. Every counter row for the same dispatch repeats the same
    (start, end) pair, so dedup by (kernel_name, dispatch_id) before summing
    to avoid multiplying duration by however many counters were collected.
    """  # noqa: D205  # tracked: #288
    seen: dict[tuple[str, str], tuple[float, float]] = {}
    for row in rows:
        if row.start_ns is None or row.end_ns is None or not row.dispatch_id:
            continue
        seen[(row.kernel_name, row.dispatch_id)] = (row.start_ns, row.end_ns)
    totals: dict[str, float] = defaultdict(float)
    for (name, _dispatch_id), (start, end) in seen.items():
        duration = end - start
        if duration > 0:
            totals[name] += duration
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
    """Bytes moved on the TCC-EA (L2-to-HBM) interface.

    Mirrors rocprofv3's own ``FETCH_SIZE``/``WRITE_SIZE`` derived-metric
    expressions (confirmed in a real MI210 ``--list-avail`` dump):
    ``FETCH_SIZE = (RDREQ_32B*32 + (RDREQ-RDREQ_32B)*64) / 1024`` and
    ``WRITE_SIZE = ((WRREQ-WRREQ_64B)*32 + WRREQ_64B*64) / 1024``. The two
    are *not* symmetric: the read side's size-breakdown counter reports the
    32B ("partial") request count directly, while the write side's reports
    the 64B ("full") request count directly. Getting this backwards silently
    inflates write bytes by ~2x when a partial breakdown is actually present.

    Real hardware counters should never report a breakdown count larger than
    the matching total request count, but nothing guarantees that (measurement
    races, a corrupted/partial capture, or a stitched-together file merging
    counters from different passes/dispatches). Clamp the derived complementary
    count at 0 instead of letting it go negative, which would otherwise make
    this function's result -- and everything derived from it (achieved
    bandwidth) -- silently negative instead of just imprecise.
    """
    total = 0.0
    found_any = False

    rd_req = _lookup(counters, "hbm_rdreq")
    if rd_req is not None:
        found_any = True
        rd_partial_32b = _lookup(counters, "hbm_rdreq_32b") or 0.0
        rd_full_64b = max(0.0, rd_req - rd_partial_32b)
        total += rd_full_64b * 64 + rd_partial_32b * 32

    wr_req = _lookup(counters, "hbm_wrreq")
    if wr_req is not None:
        found_any = True
        wr_full_64b = _lookup(counters, "hbm_wrreq_64b")
        # No size breakdown captured: conservatively treat every write
        # request as a full 64B line, matching the read side's default.
        wr_full_64b = wr_req if wr_full_64b is None else wr_full_64b
        wr_partial_32b = max(0.0, wr_req - wr_full_64b)
        total += wr_full_64b * 64 + wr_partial_32b * 32

    return total if found_any else None


def _mfma_instruction_count(counters: dict[str, float]) -> float | None:
    """Prefer the total-instruction counter (``SQ_INSTS_MFMA``) over the per-dtype ``MOPS_*`` counters.

    ``SQ_INSTS_MFMA`` is the counter a real MI210 capture used and is a
    straight instruction count. The ``MOPS_*`` counters count math
    *operations* (already divided by 512, per AMD's description) in a
    different unit, so they are only summed as a fallback when the total
    counter wasn't captured -- never added to it.
    """
    total_insts = _lookup(counters, "mfma_insts_total")
    if total_insts is not None:
        return total_insts
    dtype_sum = sum(v for k in MFMA_CANDIDATE_KEYS if (v := _lookup(counters, k)) is not None)
    return dtype_sum if dtype_sum > 0 else None


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

    mfma_total = _mfma_instruction_count(counters)
    # Prefer total cycles (GRBM_COUNT) as the rate denominator; a real MI210
    # capture that includes SQ_INSTS_MFMA didn't also request GRBM_COUNT (4
    # counters is the pass budget), so fall back to busy cycles
    # (GRBM_GUI_ACTIVE) when total isn't present. That changes the rate from
    # "instructions per elapsed cycle" to "instructions per busy cycle" --
    # if anything a tighter compute-bound signal, since it excludes idle gaps.
    cycles_for_mfma_rate = total if total is not None and total > 0 else busy
    if (
        mfma_total is not None
        and mfma_total > 0
        and cycles_for_mfma_rate is not None
        and cycles_for_mfma_rate > 0
    ):
        metrics.mfma_issue_rate = mfma_total / cycles_for_mfma_rate

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


def _plan_command(*, out_dir: str, cset: CounterSet, ns: argparse.Namespace) -> str:
    command_tokens = [t for t in ns.command if t != "--"]
    command = shlex.join(command_tokens) if command_tokens else "<your_command_and_args>"
    # --output-format csv only: a real rocprofv3 6.4.1 JSON capture is a
    # normalized/relational schema report/triage cannot parse (see module
    # docstring), so asking for `json` alongside `csv` only wastes output.
    # No `-o`: unverified against real hardware, and `-d` alone already
    # gives each pass its own directory.
    lines = [f"rocprofv3 --pmc {' '.join(cset.counters)} --output-format csv"]
    if ns.kernel:
        lines.append(f"    --kernel-include-regex {shlex.quote(ns.kernel)}")
    lines.append(f"    -d {out_dir}")
    lines.append(f"    -- {command}")
    return " \\\n".join(lines)


def _plan_one_set(*, arch: str, set_name: str, cset: CounterSet, ns: argparse.Namespace) -> None:
    out_dir = f"{ns.out_dir}/{arch}/{set_name}"
    flag = "verified" if cset.verified else "UNVERIFIED -- confirm with `rocprofv3 --list-avail`"
    print(f"\n# {set_name} [{flag}]")  # noqa: T201  # tracked: #288
    if cset.note:
        print(f"# {cset.note}")  # noqa: T201  # tracked: #288
    print(_plan_command(out_dir=out_dir, cset=cset, ns=ns))  # noqa: T201  # tracked: #288


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
        "\n# Note: rocprofv3 nests output under <out_dir>/pmc_1/<hostname>/<pid>_*, not "
        "directly under <out_dir> (confirmed on real MI210 hardware) -- report/triage "
        "discover files recursively so this doesn't matter.\n"
        "# Merge and analyze with:\n"
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


KERNEL_NAME_PRINT_LIMIT = 100


def _short_kernel_name(name: str) -> str:
    """Truncate a kernel name for display.

    Real Tensile/rocBLAS GEMM names run a few hundred characters and
    PyTorch's templated RNG-fill kernels can run several thousand (full
    ``distribution_elementwise_grid_stride_kernel<...>`` template
    signatures observed on a real MI210 capture); printing them in full
    blows up report/triage output far past what's useful in an agent
    prompt. Aggregation and filtering still key off the untruncated name
    (see ``KernelAgg``/``_filter_kernels``) -- only display is shortened.
    """
    if len(name) <= KERNEL_NAME_PRINT_LIMIT:
        return name
    return name[: KERNEL_NAME_PRINT_LIMIT - 1] + "…"


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
            " (no duration data: no Start/End_Timestamp on the PMC rows and no kernel_trace file)"
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
    # Prefer a dedicated kernel_trace capture (PMC-overhead-free) over the
    # PMC rows' own timestamps when both are present.
    durations = _duration_from_counter_rows(rows)
    durations.update(_load_kernel_trace_durations(ns.dirs))
    aggs = _aggregate_by_kernel(rows)
    kernels = _filter_kernels(aggs, ns.kernel)
    arch = _report_arch_for(ns)

    print(f"Merged {len(counter_files)} counter file(s), {len(kernels)} kernel(s) matched.")  # noqa: T201  # tracked: #288
    if durations:
        print(f"Duration data available for {len(durations)} kernel name(s).")  # noqa: T201  # tracked: #288

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
        print(f"\n{_short_kernel_name(agg.name)}  ({agg.dispatch_count} dispatch(es))")  # noqa: T201  # tracked: #288
        _print_kernel_resources(agg, occ)
        _print_derived(metrics)

    remainder = kernels[ns.top :]
    if remainder:
        print(f"\n( +{len(remainder)} more kernel(s) not shown; raise --top )")  # noqa: T201  # tracked: #288


OCCUPANCY_LOW_WAVES_PER_CU = 8  # out of a max of 32 (8 waves/SIMD * 4 SIMD/CU)
BANDWIDTH_BOUND_FRACTION_OF_PEAK = 0.5
MFMA_ISSUE_RATE_COMPUTE_BOUND = 0.05
LDS_BANK_CONFLICT_BOUND_PCT = 5.0
SMALL_GRID_CU_MULTIPLE = 2


@dataclass(frozen=True)
class Verdict:
    """One kernel's bottleneck classification with its supporting evidence and next lever."""

    label: str
    evidence: str
    lever: str


def _classify(
    *, agg: KernelAgg, occ: Occupancy, metrics: DerivedMetrics, spec: PeakSpec
) -> Verdict:  # tracked: #288
    if occ.waves_per_cu < OCCUPANCY_LOW_WAVES_PER_CU:
        return Verdict(
            "OCCUPANCY-LIMITED",
            f"{occ.waves_per_cu} waves/CU (max 32), bound by {occ.bound_by} "
            f"(vgpr={agg.vgpr_count} sgpr={agg.sgpr_count} lds={agg.lds_block_size}B)",
            f"reduce {occ.bound_by} usage per thread, or shrink the workgroup, to raise waves/CU",
        )
    if metrics.achieved_bw_gb_s is not None:
        bw_fraction = metrics.achieved_bw_gb_s / (spec.hbm_tb_s * 1000)
        if bw_fraction >= BANDWIDTH_BOUND_FRACTION_OF_PEAK:
            return Verdict(
                "BANDWIDTH-BOUND",
                f"achieved {metrics.achieved_bw_gb_s:.0f} GB/s "
                f"({bw_fraction * 100:.0f}% of {spec.hbm_tb_s * 1000:.0f} GB/s spec peak)",
                "raise arithmetic intensity: fuse epilogues, tile for L2/Infinity-Cache reuse, check XCD locality",
            )
    if (
        metrics.mfma_issue_rate is not None
        and metrics.mfma_issue_rate >= MFMA_ISSUE_RATE_COMPUTE_BOUND
    ):
        busy = _fmt(metrics.gpu_busy_pct, "%", 1)
        return Verdict(
            "COMPUTE-BOUND",
            f"MFMA issue rate {metrics.mfma_issue_rate:.4f} insts/cycle, GPU busy {busy}",
            "tune MFMA shape/wave scheduling, or move to a lower-precision path (FP8/FP6/FP4) before "
            "grinding further -- ~45-55% of the dense TFLOP/s peak is the practical ceiling for tuned GEMM",
        )
    if (
        metrics.lds_bank_conflict_rate_pct is not None
        and metrics.lds_bank_conflict_rate_pct >= LDS_BANK_CONFLICT_BOUND_PCT
    ):
        return Verdict(
            "LDS-BOUND",
            f"LDS bank-conflict rate {metrics.lds_bank_conflict_rate_pct:.1f}%",
            "change the LDS access pattern (padding, stride) to reduce bank conflicts",
        )
    if agg.grid_size and agg.grid_size < spec.compute_units * SMALL_GRID_CU_MULTIPLE:
        return Verdict(
            "LAUNCH/UNDER-FILLED",
            f"grid_size={agg.grid_size} vs. {spec.compute_units} CUs "
            f"({agg.grid_size / spec.compute_units:.2f} workgroups/CU)",
            "batch more work per launch, fuse dispatches, or use HIP graphs to cut launch overhead",
        )
    return Verdict(
        "LATENCY-BOUND",
        f"occupancy fine ({occ.waves_per_cu} waves/CU) but far from both roofs",
        "capture an ATT trace (att.py) to find the stalling instructions and deepen the pipeline / "
        "prefetch distance",
    )


def _print_peak_table(spec: PeakSpec, arch: str) -> None:
    print(  # noqa: T201  # tracked: #288
        f"Peak constants for {arch} ({spec.label}) -- SPEC PEAKS, not an achievable ceiling:"
    )
    print(  # noqa: T201  # tracked: #288
        f"  {spec.compute_units} CUs, {spec.dense_bf16_fp16_tflops:.1f} TFLOP/s dense bf16/fp16, "
        f"{spec.hbm_tb_s:.1f} TB/s HBM, {spec.hbm_gb} GB   (ridge: {spec.ridge_flop_per_byte:.0f} FLOP/byte)"
    )
    print(  # noqa: T201  # tracked: #288
        "  Practical ceiling for tuned GEMM is ~45-55% of the compute peak, and clean streaming "
        "reads land ~50-60% of the HBM peak -- do not treat the spec numbers as achievable."
    )
    if arch == "gfx942":
        print(f"  {PEAK_ARCH_NOTE}")  # noqa: T201  # tracked: #288


def _peak_spec_for(arch: str) -> tuple[str, PeakSpec]:
    family = _normalize_arch_or_exit(arch)
    if family not in PEAK_SPECS:
        known = ", ".join(sorted(PEAK_SPECS))
        sys.exit(f"no peak spec for {family!r}; known families: {known}")
    return family, PEAK_SPECS[family]


def cmd_triage(ns: argparse.Namespace) -> None:
    """Classify each hot kernel as one of five bottleneck verdicts with evidence."""
    arch, spec = _peak_spec_for(ns.arch)
    counter_files = _discover(ns.dirs, "counter_collection", (".csv", ".json"))
    if not counter_files:
        print("(no *counter_collection*.csv/.json files found under the given directories)")  # noqa: T201  # tracked: #288
        return
    rows = _load_counter_rows(counter_files)
    durations = _duration_from_counter_rows(rows)
    durations.update(_load_kernel_trace_durations(ns.dirs))
    aggs = _aggregate_by_kernel(rows)
    kernels = _filter_kernels(aggs, ns.kernel)

    _print_peak_table(spec, arch)
    for agg in kernels[: ns.top]:
        occ = resource_occupancy(
            vgpr_count=agg.vgpr_count,
            sgpr_count=agg.sgpr_count,
            lds_block_size=agg.lds_block_size,
            workgroup_size=agg.workgroup_size,
            arch=arch,
        )
        metrics = derive_metrics(agg, durations.get(agg.name))
        verdict = _classify(agg=agg, occ=occ, metrics=metrics, spec=spec)
        print(f"\n{_short_kernel_name(agg.name)}")  # noqa: T201  # tracked: #288
        print(f"  verdict: {verdict.label}")  # noqa: T201  # tracked: #288
        print(f"  evidence: {verdict.evidence}")  # noqa: T201  # tracked: #288
        print(f"  next lever: {verdict.lever}")  # noqa: T201  # tracked: #288


def _add_dirs_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dirs", nargs="+", help="One or more PMC pass output directories")
    parser.add_argument("--kernel", default=None, help="Regex filter on Kernel_Name")
    parser.add_argument("--top", type=int, default=15)


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="counters",
        description="rocprofv3 PMC counter-set catalogue, report aggregation, and bottleneck triage.",
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

    triage = sub.add_parser("triage", help="classify each hot kernel's bottleneck with evidence")
    _add_dirs_arg(triage)
    triage.add_argument("--arch", required=True)
    triage.set_defaults(fn=cmd_triage)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
