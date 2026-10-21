"""Host-side maintenance commands, dispatched before starting a run engine."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.api.host import host_call
from vibesys.api.transport import ConversionStatus, convert_run, convert_runs
from vs_project.api import Project, ProjectError
from vs_sim.api import OsProcesses, OsThreads, SystemClock

if TYPE_CHECKING:
    from typing import BinaryIO

    from vs_sim.api import Processes, Threads


def run(argv: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run a host maintenance method and return exit code, stdout and stderr."""
    parser = argparse.ArgumentParser(
        prog="vibesys host",
        epilog="post-intent reads a HostCallRequest from stdin and writes a HostCallResponse to stdout.",
    )
    methods = parser.add_subparsers(dest="method", required=True)
    convert = methods.add_parser("convert-run", help="convert a stopped run's JSONL journal")
    convert.add_argument("run", help="run id")
    convert.add_argument("--project", type=Path, default=cwd, help="project directory")
    bulk = methods.add_parser("convert-runs", help="convert stopped project and host-index runs")
    bulk.add_argument("--project", type=Path, default=cwd, help="project directory")
    args = parser.parse_args(argv)
    root = args.project if args.project.is_absolute() else cwd / args.project
    try:
        project = Project.open(root)
        if args.method == "convert-runs":
            rows = convert_runs(project, clock=SystemClock(), threads=OsThreads())
            counts = Counter(row.result.status for row in rows)
            output = "".join(
                f"{row.run}: {row.result.status}: {row.result.reason}\n" for row in rows
            )
            summary = ", ".join(f"{status}={counts[status]}" for status in ConversionStatus)
            return int(counts[ConversionStatus.FAILED] > 0), output + summary + "\n", ""
        result = convert_run(project, args.run, clock=SystemClock(), threads=OsThreads())
    except (OSError, ValueError, TypeError, ProjectError) as exc:
        return 1, "", f"vibesys host {args.method}: {exc}\n"
    return 0, f"{result.status}: {result.reason}\n", ""


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    processes: Processes | None = None,
    threads: Threads | None = None,
) -> int:
    """Run one host method from the current directory."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] in (["post-intent"], ["relaunch"]):
        parser = argparse.ArgumentParser(
            prog="vibesys host post-intent",
            description="Read a binary HostCallRequest from stdin; write a binary HostCallResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(
            host_call(
                source.read((1 << 20) + 1),
                processes=OsProcesses() if processes is None else processes,
                threads=OsThreads() if threads is None else threads,
            )
        )
        return 0
    code, output, error = run(arguments, cwd=Path.cwd())
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
