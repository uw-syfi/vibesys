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
import shlex
import sys
from dataclasses import dataclass

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


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="counters",
        description="rocprofv3 PMC counter-set catalogue and plan command.",
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

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
