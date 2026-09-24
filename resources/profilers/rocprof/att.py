#!/usr/bin/env python3
"""rocprofv3 Advanced Thread Trace (ATT) capture planning.

ATT gives per-instruction stall/latency timing but no cache counters (use
``counters.py`` for L2/HBM PMC). It cannot be combined with PMC in one
rocprofv3 job -- capture them in separate passes.

Usage:
    python att.py plan --arch gfx90a --kernel REGEX

``plan`` prints a rocprofv3 ATT job config (rocprofv3's ATT options are only
exposed through ``-i <input.yaml>``, unlike PMC's ``--pmc`` CLI flag) plus the
prerequisites: a matching ``rocprof-trace-decoder`` shared library findable by
rocprofv3 (via ``$ROCM_PATH/lib`` or ``ROCPROF_TRACE_DECODER_PATH``).
"""  # noqa: EXE001  # tracked: #288

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

DEFAULT_TARGET_CU = 1
DEFAULT_BUFFER_SIZE = "0x6000000"  # 96MB/SE; raise to 0xC000000 if traces truncate
DEFAULT_SE_MASK = "0xf"
DEFAULT_SIMD_SELECT = "0xf"
DEFAULT_ITERATION_RANGE = "[1, [2-4]]"  # skip warmup (iteration 0), trace 2-4


def _plan_yaml(ns: argparse.Namespace) -> str:
    lines = [
        "jobs:",
        "  -",
        f"      kernel_include_regex: {ns.kernel!r}",
        f"      kernel_iteration_range: {ns.iteration_range!r}",
        "      output_file: att_out",
        f"      output_directory: {ns.out_dir}",
        "      output_format: [csv]",
        "      truncate_kernels: true",
        "      sys_trace: true",
        "      advanced_thread_trace: true",
        f"      att_target_cu: {ns.target_cu}",
        f"      att_shader_engine_mask: {ns.se_mask!r}",
        f"      att_simd_select: {ns.simd_select!r}",
        f"      att_buffer_size: {ns.buffer_size!r}",
    ]
    return "\n".join(lines)


def cmd_plan(ns: argparse.Namespace) -> None:
    """Print a rocprofv3 ATT job config and the invocation to run it."""
    yaml_path = ns.yaml_path
    command_tokens = [t for t in ns.command if t != "--"]
    command = shlex.join(command_tokens) if command_tokens else "<your_command_and_args>"

    print(f"# ATT job config for {ns.arch} ({yaml_path}):")  # noqa: T201  # tracked: #288
    print(_plan_yaml(ns))  # noqa: T201  # tracked: #288
    if ns.write:
        Path(yaml_path).write_text(_plan_yaml(ns) + "\n", encoding="utf-8")
        print(f"\n# wrote {yaml_path}")  # noqa: T201  # tracked: #288

    print(  # noqa: T201  # tracked: #288
        "\n# Run (build/compile the kernel with debug info enabled first -- whatever your "
        "toolchain's flag for embedding DWARF source-to-assembly mapping is -- or code.json's "
        "source_loc will come back empty):"
    )
    print(f"rocprofv3 -i {yaml_path} -- {command}")  # noqa: T201  # tracked: #288

    print("\n# Prerequisites:")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        f"#  - {ns.arch}: the ATT job options (target CU, buffer size, SE/SIMD masks) below are "
        "generic across CDNA gfx9 and are not varied by architecture here; confirm the decoder "
        "and ROCm build both match this GPU."
    )
    print("#  - rocprof-trace-decoder shared library installed and discoverable: place it under")  # noqa: T201  # tracked: #288
    print("#    $ROCM_PATH/lib, or point ROCPROF_TRACE_DECODER_PATH at it. Without it, ATT jobs")  # noqa: T201  # tracked: #288
    print("#    produce raw trace data but no decoded code.json (see `hotspots` for the error).")  # noqa: T201  # tracked: #288
    print("#  - att_target_cu keeps output to one CU; raise att_buffer_size (e.g. 0xC000000) if")  # noqa: T201  # tracked: #288
    print("#    the decoded trace reports truncation.")  # noqa: T201  # tracked: #288
    print(  # noqa: T201  # tracked: #288
        f"#  - Output lands under {ns.out_dir}/ in a ui_output_agent_<PID>_dispatch_<N> directory "
        "per matched dispatch; pass that directory (or its parent) to `hotspots`."
    )


def main(argv: list[str] | None = None) -> None:  # noqa: D103  # tracked: #288
    parser = argparse.ArgumentParser(
        prog="att",
        description="rocprofv3 ATT capture planning.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print a rocprofv3 ATT job config and invocation")
    plan.add_argument(
        "--arch", required=True, help="e.g. gfx90a, mi210, gfx942, mi300x, gfx950, mi355x"
    )
    plan.add_argument("--kernel", required=True, help="kernel_include_regex value")
    plan.add_argument("--target-cu", type=int, default=DEFAULT_TARGET_CU)
    plan.add_argument("--buffer-size", default=DEFAULT_BUFFER_SIZE)
    plan.add_argument("--se-mask", default=DEFAULT_SE_MASK)
    plan.add_argument("--simd-select", default=DEFAULT_SIMD_SELECT)
    plan.add_argument("--iteration-range", default=DEFAULT_ITERATION_RANGE)
    plan.add_argument("--out-dir", default="rocprof_att")
    plan.add_argument("--yaml-path", default="rocprof_att.yaml")
    plan.add_argument("--write", action="store_true", help="also write the config to --yaml-path")
    plan.add_argument("command", nargs=argparse.REMAINDER, help="the program to profile, after --")
    plan.set_defaults(fn=cmd_plan)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
