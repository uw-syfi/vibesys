"""Host-side maintenance commands, dispatched before starting a run engine."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.api.host import host_call
from vibesys.api.transport import convert_run
from vs_project.api import Project, ProjectError

if TYPE_CHECKING:
    from typing import BinaryIO


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
    args = parser.parse_args(argv)
    root = args.project if args.project.is_absolute() else cwd / args.project
    try:
        result = convert_run(Project.open(root), args.run)
    except (OSError, ValueError, ProjectError) as exc:
        return 1, "", f"vibesys host convert-run: {exc}\n"
    return 0, f"{result.status}: {result.reason}\n", ""


def main(
    argv: list[str] | None = None, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None
) -> int:
    """Run one host method from the current directory."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["post-intent"]:
        parser = argparse.ArgumentParser(
            prog="vibesys host post-intent",
            description="Read a binary HostCallRequest from stdin; write a binary HostCallResponse to stdout.",
        )
        parser.parse_args(arguments[1:])
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        destination.write(host_call(source.read((1 << 20) + 1)))
        return 0
    code, output, error = run(arguments, cwd=Path.cwd())
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
