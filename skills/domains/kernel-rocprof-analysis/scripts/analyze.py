#!/usr/bin/env python3
"""Analyze rocprofv3, rocprof-compute and torch.profiler captures on this machine.

Every tool here reads capture files that a profiling job copied back; none of them
needs a GPU, ROCm or the VibeSys package (``compute analyze`` uses
``rocprof-compute`` when it is installed and reads the raw CSVs otherwise). Python
3.10 or newer, standard library only.

Usage:
    analyze.py trace    files|kernels|families|idle_gaps|cpu_overhead|memory|graphs|
                        host_idle|completeness|query|summary <report> [options]
    analyze.py counters list-sets|plan|report|triage [options]
    analyze.py att      plan|hotspots [options]
    analyze.py compute  doctor|analyze [options]
    analyze.py compare  timeline <a> <b> | counters --arch A --a DIR... --b DIR...
    analyze.py torch    certify|gemm-shapes|roofline|kernels|operators|cpu-overhead|
                        memory|tables|summary <trace> [options]
    analyze.py bench    parse|compare|verdict [options]

``analyze.py <group> --help`` lists a group's commands and options. Exit status: 0
when the analysis ran, 1 when an input is not a readable report (the message names
it), 2 for a usage error.
"""

from __future__ import annotations

import csv
import importlib
import sqlite3
import sys
from pathlib import Path

# The group modules sit beside this file; make them importable however it was started.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from analysis_errors import AnalysisError  # noqa: E402


def _import_group(module_name: str) -> object:
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    return importlib.import_module(module_name)


# Each group is one module beside this file, imported only when its group runs.
GROUPS = {
    "trace": "rocprof_trace",
    "counters": "rocprof_counters",
    "att": "rocprof_att",
    "compute": "rocprof_compute",
    "compare": "rocprof_compare",
    "torch": "torch_trace",
    "bench": "bench_stats",
}
EXIT_UNREADABLE_INPUT = 1
EXIT_USAGE = 2

# Errors that mean "this input is not a readable report", reported as one line.
_INPUT_ERRORS = (AnalysisError, OSError, ValueError, sqlite3.DatabaseError, csv.Error)


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``<group> <args...>`` to the group's module; return the exit status."""
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(__doc__ or "")
        return 0 if args else EXIT_USAGE
    group, rest = args[0], args[1:]
    module_name = GROUPS.get(group)
    if module_name is None:
        sys.stderr.write(f"error: unknown group {group!r}; choose from: {', '.join(GROUPS)}\n")
        return EXIT_USAGE
    module = _import_group(module_name)
    try:
        module.main(rest)
    except SystemExit as exc:
        # argparse exits with an int; a tool that rejects its input exits with a message.
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        sys.stderr.write(f"error: {exc.code}\n")
        return EXIT_UNREADABLE_INPUT
    except _INPUT_ERRORS as exc:
        message = str(exc)
        prefix = "" if message.startswith("error:") else "error: "
        sys.stderr.write(f"{prefix}{message}\n")
        return EXIT_UNREADABLE_INPUT
    return 0


if __name__ == "__main__":
    sys.exit(main())
