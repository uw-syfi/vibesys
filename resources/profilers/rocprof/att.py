#!/usr/bin/env python3
"""rocprofv3 Advanced Thread Trace (ATT) capture planning and hotspot analysis.

ATT gives per-instruction stall/latency timing but no cache counters (use
``counters.py`` for L2/HBM PMC). It cannot be combined with PMC in one
rocprofv3 job -- capture them in separate passes.

Usage:
    python att.py plan --arch gfx90a --kernel REGEX --decoder-lib-dir <dir>
    python att.py hotspots <dispatch_dir> [--top 15]

``plan`` prints a rocprofv3 ATT command line -- all ATT options (target CU,
buffer size, SE/SIMD masks, the decoder library path) are plain CLI flags on
``rocprofv3`` itself, verified on ROCm 7.2.0's ``rocprofv3 --help``; no
``-i <input.yaml>`` job config is needed for ATT. Prerequisites: ROCm's
``rocprofv3`` must be >= 7.1 (6.x has no ``--att`` flag at all) and the
separate ``rocprof-trace-decoder`` shared library (not part of any ROCm
module) must be extracted somewhere and passed via ``--att-library-path
<dir containing librocprof-trace-decoder.so>`` -- verified working on an
MI210/gfx90a cluster with the decoder's ``0.1.6`` GitHub release tarball.

``hotspots`` reads the decoder's per-dispatch output directory (conventionally
named ``ui_output_agent_<PID>_dispatch_<N>``), specifically ``code.json``.
Verified against real MI210 captures: each row is
``[asm, _, pc_index, source_loc, codeobj_id, pc_addr, exec_count,
total_cycles, stall_cycles, idle_cycles]`` -- ``code.json``'s own ``header``
field documents this as ``"ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit,
Latency, Stall, Idle"``, confirmed independently by the sibling
``stats_ui_output_agent_<PID>_dispatch_<N>.csv``. Columns are resolved by
this header's own NAMES, not by position: decoder releases are not
guaranteed to keep the column order stable, and a positional reader would
silently mismap data if it ever changes. When ``code.json`` carries no
``header`` field at all (older/hand-built output), the documented order
above is used as a fallback; when a ``header`` field is present but missing
a column this toolkit needs, parsing fails with a clear error instead of
guessing. It prints the top instructions and top source lines by stall
cycles, plus stall-category totals, and fails with a clear message when
``code.json`` is missing (decoder not run, or wrong directory).
"""  # noqa: EXE001  # tracked: #288

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CODE_JSON_NAME = "code.json"
DEFAULT_TARGET_CU = 1
# Plain decimal integer byte count -- verified. Unit-suffixed strings ("64MB") fail with a
# Python `ValueError: invalid literal for int()` inside rocprofv3; raise if traces truncate.
DEFAULT_BUFFER_SIZE = 67_108_864  # 64MB
DEFAULT_SE_MASK = "0x1"
DEFAULT_SIMD_SELECT = "0xf"

STALL_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("s_barrier", "barrier"),
    ("s_wait_idle", "barrier"),
    ("vmcnt", "VMEM-wait"),
    ("lgkmcnt", "LDS/SMEM-wait"),
    ("expcnt", "EXP-wait"),
    ("s_waitcnt", "waitcnt"),
    ("buffer_load", "VMEM-load"),
    ("global_load", "VMEM-load"),
    ("flat_load", "VMEM-load"),
    ("buffer_store", "VMEM-store"),
    ("global_store", "VMEM-store"),
    ("ds_read", "LDS"),
    ("ds_write", "LDS"),
    ("s_load", "SMEM"),
    ("s_store", "SMEM"),
    ("v_mfma", "MFMA/FMA"),
    ("v_fma", "MFMA/FMA"),
)


def _stall_category(asm: str) -> str:
    lowered = asm.lower()
    for needle, category in STALL_CATEGORIES:
        if needle in lowered:
            return category
    return "other"


@dataclass(frozen=True)
class Instruction:
    """One decoded ISA instruction with its aggregated per-wave stall stats."""

    asm: str
    pc_index: int
    source_loc: str
    pc_addr: int
    exec_count: int
    total_cycles: int
    stall_cycles: int
    idle_cycles: int

    @property
    def stall_pct(self) -> float:
        """Fraction of this instruction's total cycles that were stall cycles."""
        return 100.0 * self.stall_cycles / self.total_cycles if self.total_cycles else 0.0

    @property
    def category(self) -> str:
        """Coarse stall category inferred from the ISA mnemonic."""
        return _stall_category(self.asm)


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) else 0


class AttOutputNotFoundError(RuntimeError):
    """Raised when no code.json is found, or its rows can't be mapped to known columns."""


# code.json's own documented `header` field (verified against a real MI210
# decoder output): "ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit, Latency,
# Stall, Idle". Maps each known header column name to the Instruction field
# it carries; "_" and "Codeobj" are unused. Resolving by NAME (not position)
# is what makes parsing robust to a decoder release reordering columns.
_HEADER_FIELD_MAP: dict[str, str] = {
    "ISA": "asm",
    "LineNumber": "pc_index",
    "Source": "source_loc",
    "Vaddr": "pc_addr",
    "Hit": "exec_count",
    "Latency": "total_cycles",
    "Stall": "stall_cycles",
    "Idle": "idle_cycles",
}
_REQUIRED_INSTRUCTION_FIELDS = frozenset(_HEADER_FIELD_MAP.values())

# Positional fallback for code.json output with no "header" field at all
# (the hand-built, docs-derived test fixture predates the decoder always
# emitting one; real decoder output always has it).
_DEFAULT_HEADER_COLUMNS: tuple[str, ...] = (
    "ISA",
    "_",
    "LineNumber",
    "Source",
    "Codeobj",
    "Vaddr",
    "Hit",
    "Latency",
    "Stall",
    "Idle",
)
_DEFAULT_INDICES: dict[str, int] = {
    field: _DEFAULT_HEADER_COLUMNS.index(col) for col, field in _HEADER_FIELD_MAP.items()
}


def _column_indices(columns: tuple[str, ...]) -> dict[str, int]:
    """Map each Instruction field to its index in ``columns``, resolved by column NAME."""
    indices = {
        field: i
        for i, col in enumerate(columns)
        for field in (_HEADER_FIELD_MAP.get(col),)
        if field is not None
    }
    missing = _REQUIRED_INSTRUCTION_FIELDS - set(indices)
    if missing:
        msg = (
            f"code.json header {list(columns)!r} is missing required column(s) for: "
            f"{sorted(missing)} (known column names: {sorted(_HEADER_FIELD_MAP)})"
        )
        raise AttOutputNotFoundError(msg)
    return indices


def _parse_header(header: object) -> dict[str, int]:
    """Resolve code.json's ``header`` field into a field->index map.

    ``header`` is a comma-separated column-name string, e.g. ``"ISA, _,
    LineNumber, ..."``. Falls back to the documented default column order
    when no header field is present at all.
    """
    if not header or not isinstance(header, str):
        return _DEFAULT_INDICES
    columns = tuple(part.strip() for part in header.split(","))
    return _column_indices(columns)


def _instruction_from_row(  # tracked: #288
    row: list, indices: dict[str, int] = _DEFAULT_INDICES
) -> Instruction | None:
    pc_i = indices["pc_index"]
    if len(row) <= max(indices.values()) or not isinstance(row[pc_i], int) or row[pc_i] == 0:
        return None
    source_loc = row[indices["source_loc"]]
    return Instruction(
        asm=str(row[indices["asm"]]),
        pc_index=row[pc_i],
        source_loc=str(source_loc) if source_loc else "<unknown>",
        pc_addr=_int_or_zero(row[indices["pc_addr"]]),
        exec_count=_int_or_zero(row[indices["exec_count"]]),
        total_cycles=_int_or_zero(row[indices["total_cycles"]]),
        stall_cycles=_int_or_zero(row[indices["stall_cycles"]]),
        idle_cycles=_int_or_zero(row[indices["idle_cycles"]]),
    )


def _find_code_json(dispatch_dir: Path) -> Path:
    direct = dispatch_dir / CODE_JSON_NAME
    if direct.is_file():
        return direct
    # Tolerate being pointed at a parent output_directory that contains one or
    # more ui_output_agent_*_dispatch_* subdirectories.
    nested = sorted(dispatch_dir.glob(f"*/{CODE_JSON_NAME}"))
    if len(nested) == 1:
        return nested[0]
    if len(nested) > 1:
        names = ", ".join(p.parent.name for p in nested)
        msg = (
            f"multiple dispatch dirs with {CODE_JSON_NAME} under {dispatch_dir}: {names}; "
            "pass the specific dispatch directory"
        )
        raise AttOutputNotFoundError(msg)
    msg = (
        f"no {CODE_JSON_NAME} found under {dispatch_dir} (or its immediate subdirectories). "
        "This means the ATT decoder did not run or produced no output for this kernel: "
        "confirm rocprof-trace-decoder is installed (see `att.py plan`), that "
        "--kernel-include-regex matched the kernel, and that the job actually ran "
        "advanced_thread_trace, not just sys_trace."
    )
    raise AttOutputNotFoundError(msg)


def load_instructions(dispatch_dir: Path) -> list[Instruction]:
    """Load and parse every instruction row from ``code.json`` in ``dispatch_dir``."""
    code_json = _find_code_json(dispatch_dir)
    try:
        data = json.loads(code_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not parse {code_json}: {exc}"
        raise AttOutputNotFoundError(msg) from exc
    rows = data.get("code") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        msg = f"{code_json} does not have the expected top-level 'code' list"
        raise AttOutputNotFoundError(msg)
    indices = _parse_header(data.get("header") if isinstance(data, dict) else None)
    instructions = (_instruction_from_row(row, indices) for row in rows if isinstance(row, list))
    return [i for i in instructions if i is not None]


@dataclass
class SourceHotspot:
    """Stall cycles aggregated across every instruction mapped to one source line."""

    source_loc: str
    total_stall_cycles: int = 0
    total_cycles: int = 0
    instruction_count: int = 0
    categories: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def dominant_category(self) -> str:
        """Stall category with the largest share of this source line's stall cycles."""
        return max(self.categories, key=self.categories.get) if self.categories else "other"


def aggregate_by_source(instructions: list[Instruction]) -> list[SourceHotspot]:
    """Group instructions by ``source_loc`` and sum their stall cycles, ranked descending."""
    by_loc: dict[str, SourceHotspot] = {}
    for inst in instructions:
        hs = by_loc.setdefault(inst.source_loc, SourceHotspot(source_loc=inst.source_loc))
        hs.total_stall_cycles += inst.stall_cycles
        hs.total_cycles += inst.total_cycles
        hs.instruction_count += 1
        if inst.stall_cycles > 0:
            hs.categories[inst.category] += inst.stall_cycles
    return sorted(by_loc.values(), key=lambda h: h.total_stall_cycles, reverse=True)


def stall_category_totals(instructions: list[Instruction]) -> list[tuple[str, int]]:
    """Total stall cycles per stall category, ranked descending."""
    totals: dict[str, int] = defaultdict(int)
    for inst in instructions:
        if inst.stall_cycles > 0:
            totals[inst.category] += inst.stall_cycles
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _fmt_cycles(n: int) -> str:
    if n >= 1_000_000:  # noqa: PLR2004  # tracked: #288
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:  # noqa: PLR2004  # tracked: #288
        return f"{n / 1_000:.1f}K"
    return str(n)


def cmd_hotspots(ns: argparse.Namespace) -> None:
    """Top instructions and source lines by stall cycles, plus stall-category totals."""
    dispatch_dir = Path(ns.dispatch_dir)
    if not dispatch_dir.is_dir():
        sys.exit(f"not a directory: {dispatch_dir}")
    try:
        instructions = load_instructions(dispatch_dir)
    except AttOutputNotFoundError as exc:
        sys.exit(str(exc))
    if not instructions:
        print("(code.json parsed but no instructions with a nonzero pc_index were found)")  # noqa: T201  # tracked: #288
        return

    total_stall = sum(i.stall_cycles for i in instructions)
    total_cycles = sum(i.total_cycles for i in instructions)
    print(f"Instructions: {len(instructions)}  Total cycles: {_fmt_cycles(total_cycles)}")  # noqa: T201  # tracked: #288
    stall_frac = 100 * total_stall / total_cycles if total_cycles else 0
    print(f"Total stall cycles: {_fmt_cycles(total_stall)} ({stall_frac:.1f}% of total cycles)")  # noqa: T201  # tracked: #288

    print("\nStall category totals:")  # noqa: T201  # tracked: #288
    for category, cycles in stall_category_totals(instructions):
        pct = 100 * cycles / total_stall if total_stall else 0
        print(f"  {category:<14} {_fmt_cycles(cycles):>8}  ({pct:5.1f}%)")  # noqa: T201  # tracked: #288

    print(f"\nTop {ns.top} instructions by stall cycles:")  # noqa: T201  # tracked: #288
    ranked = sorted(
        (i for i in instructions if i.stall_cycles > 0), key=lambda i: i.stall_cycles, reverse=True
    )
    for rank, inst in enumerate(ranked[: ns.top], 1):
        pct = 100 * inst.stall_cycles / total_stall if total_stall else 0
        asm = inst.asm if len(inst.asm) <= 48 else inst.asm[:47] + "…"  # noqa: PLR2004  # tracked: #288
        print(  # noqa: T201  # tracked: #288
            f"  #{rank:<3} {_fmt_cycles(inst.stall_cycles):>8} ({pct:4.1f}%)  {inst.category:<12}"
            f"  {asm:<48}  {inst.source_loc}"
        )

    print(f"\nTop {ns.top} source lines by aggregated stall cycles:")  # noqa: T201  # tracked: #288
    for rank, hs in enumerate(aggregate_by_source(instructions)[: ns.top], 1):
        if hs.total_stall_cycles == 0:
            break
        pct = 100 * hs.total_stall_cycles / total_stall if total_stall else 0
        print(  # noqa: T201  # tracked: #288
            f"  #{rank:<3} {_fmt_cycles(hs.total_stall_cycles):>8} ({pct:4.1f}%)  "
            f"{hs.dominant_category:<12}  {hs.instruction_count:>4} insn  {hs.source_loc}"
        )


def _plan_command_tokens(ns: argparse.Namespace) -> list[str]:
    """Build the rocprofv3 ATT invocation as a token list.

    All flags, verified on rocprofv3 (ROCm 7.2.0) --help: ATT has no separate
    `-i <input.yaml>` job-config path.
    """
    tokens = [
        "rocprofv3",
        "--att",
        "--att-target-cu",
        str(ns.target_cu),
        "--att-simd-select",
        str(ns.simd_select),
        "--att-shader-engine-mask",
        str(ns.se_mask),
        "--att-buffer-size",
        str(ns.buffer_size),
        "--att-library-path",
        ns.decoder_lib_dir or "<dir containing librocprof-trace-decoder.so>",
        "--kernel-include-regex",
        ns.kernel,
    ]
    if ns.iteration_range:
        tokens += ["--kernel-iteration-range", *ns.iteration_range]
    tokens += [
        "-d",
        ns.out_dir,
        "--output-format",
        "csv",
        "json",
        "--",
    ]
    return tokens


def cmd_plan(ns: argparse.Namespace) -> None:
    """Print a rocprofv3 ATT command line and the prerequisites to run it."""
    command_tokens = [t for t in ns.command if t != "--"]
    command = shlex.join(command_tokens) if command_tokens else "<your_command_and_args>"

    full_command = shlex.join(_plan_command_tokens(ns)) + f" {command}"

    print(f"# ATT capture command for {ns.arch}:")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        "# (build/compile the kernel with debug info enabled first -- whatever your toolchain's "
        "flag for embedding DWARF source-to-assembly mapping is, e.g. `hipcc -g` -- or "
        "code.json's source_loc will come back empty):"
    )
    print(full_command)  # noqa: T201  # tracked: #288
    if ns.write:
        Path(ns.script_path).write_text(
            "#!/bin/bash\nset -eux\n" + full_command + "\n", encoding="utf-8"
        )
        print(f"\n# wrote {ns.script_path}")  # noqa: T201  # tracked: #288

    print("\n# Prerequisites:")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        f"#  - {ns.arch}: rocprofv3 >= ROCm 7.1 -- 6.x has no --att/--advanced-thread-trace flag "
        "at all. The ATT flags themselves (target CU, buffer size, SE/SIMD masks) are generic "
        "across CDNA gfx9; confirm the decoder release and ROCm build both match this GPU."
    )
    print(  # noqa: T201  # tracked: #288
        "#  - rocprof-trace-decoder shared library: a separate release, not part of any ROCm "
        "module (github.com/ROCm/rocprof-trace-decoder). Extract librocprof-trace-decoder.so "
        "from a release tarball (no build needed) and pass its containing DIRECTORY via "
        "--att-library-path -- verified working; without it, ATT jobs fail immediately with "
        "'rocprof-trace-decoder library path not found', not a partial/undecoded result."
    )
    print(  # noqa: T201  # tracked: #288
        "#  - --att-buffer-size takes a PLAIN DECIMAL INTEGER byte count only -- a unit-suffixed "
        "string ('64MB') fails with `ValueError: invalid literal for int()`. --att-simd-select "
        "and --att-shader-engine-mask accept hex ('0xf') or decimal; both verified."
    )
    print("#  - att_target_cu keeps output to one CU; raise att_buffer_size if the decoded")  # noqa: T201  # tracked: #288
    print("#    trace reports truncation.")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        f"#  - Output lands under {ns.out_dir}/ in a ui_output_agent_<PID>_dispatch_<N> directory "
        "per matched dispatch that the decoder could resolve (a kernel can match "
        "--kernel-include-regex and still produce no code.json, e.g. degenerate fill kernels); "
        "pass that directory (or its parent) to `hotspots`."
    )


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="att",
        description="rocprofv3 ATT capture planning and hotspot analysis.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print a rocprofv3 ATT command line and prerequisites")
    plan.add_argument(
        "--arch", required=True, help="e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x"
    )
    plan.add_argument("--kernel", required=True, help="--kernel-include-regex value")
    plan.add_argument("--target-cu", type=int, default=DEFAULT_TARGET_CU)
    plan.add_argument(
        "--buffer-size",
        type=int,
        default=DEFAULT_BUFFER_SIZE,
        help="plain decimal byte count -- unit-suffixed strings ('64MB') are rejected",
    )
    plan.add_argument("--se-mask", default=DEFAULT_SE_MASK)
    plan.add_argument("--simd-select", default=DEFAULT_SIMD_SELECT)
    plan.add_argument(
        "--iteration-range",
        nargs="*",
        default=None,
        help="values passed through to --kernel-iteration-range, e.g. --iteration-range 2 3 4",
    )
    plan.add_argument("--out-dir", default="rocprof_att")
    plan.add_argument(
        "--decoder-lib-dir",
        default=None,
        help="directory containing librocprof-trace-decoder.so (--att-library-path)",
    )
    plan.add_argument("--script-path", default="rocprof_att.sh")
    plan.add_argument(
        "--write", action="store_true", help="also write the command to --script-path"
    )
    plan.add_argument("command", nargs=argparse.REMAINDER, help="the program to profile, after --")
    plan.set_defaults(fn=cmd_plan)

    hotspots = sub.add_parser("hotspots", help="top stall hotspots from a decoded ATT dispatch dir")
    hotspots.add_argument("dispatch_dir")
    hotspots.add_argument("--top", type=int, default=15)
    hotspots.set_defaults(fn=cmd_hotspots)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
