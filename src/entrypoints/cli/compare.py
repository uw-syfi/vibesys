"""``vibesys compare``: measure a task's comparison baselines once, and keep the readings.

A comparison baseline is a mature engine (vLLM, SGLang) the task declares with ``[[comparison]]``.
This command is the only thing that measures one: it runs the task's accuracy check and benchmark
against the engine, served from its pinned image on the operator's Slurm cluster, three times on
different nodes, and stores the reading in the project's machine-local cache. A run only reads that
cache. A comparison already measured for its current configuration is not measured again unless
``--force`` says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from vibesys.api.request import load_task
from vibesys.comparisons.api import SAMPLES, ComparisonHost, measure_comparisons
from vs_project.api import (
    CliGitRepository,
    Git,
    NullGitTrackerEvents,
    Project,
)
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
)
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sim.api import OsProcesses, SystemClock, ThreadBlockingRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.comparisons.api import ComparisonReport

_DEFAULT_SLURM_CONFIG = Path("~/.config/vibesys/slurm.toml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys compare",
        description="Measure the task's comparison baselines and keep the readings.",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="the project (default: .)")
    parser.add_argument("--task", help="the task, when the project has several")
    parser.add_argument(
        "--slurm-config",
        type=Path,
        default=_DEFAULT_SLURM_CONFIG,
        help="the operator's Slurm settings file (default: %(default)s)",
    )
    parser.add_argument(
        "--only", action="append", default=[], metavar="NAME", help="one comparison"
    )
    parser.add_argument("--force", action="store_true", help="measure again, even if stored")
    parser.add_argument("--samples", type=int, default=SAMPLES, help="runs per comparison")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def describe(report: ComparisonReport) -> str:
    """One line for a comparison: its label and what is known, in words and numbers only."""
    reading = report.reading
    if isinstance(reading, ComparisonMeasured):
        spread = "" if reading.spread is None else f" ±{reading.spread * 100:.1f}%"
        unit = f" {reading.unit}" if reading.unit else ""
        nodes = f" nodes {','.join(reading.hosts)}" if reading.hosts else ""
        stored = " stored" if report.cached else ""
        return (
            f"{report.label}: {reading.value:.4g}{unit}{spread} "
            f"({len(reading.samples)} samples{nodes}){stored}"
        )
    if isinstance(reading, ComparisonFailed):
        return f"{report.label}: failed: {reading.reason}"
    if isinstance(reading, ComparisonNotConfigured):
        return f"{report.label}: not configured: {reading.reason}"
    return f"{report.label}: not measured"


def run(argv: Sequence[str], out: TextIO | None = None) -> int:
    """Run one ``vibesys compare``; exit 0 unless some comparison failed or is not configured."""
    out = sys.stdout if out is None else out
    args = _parser().parse_args(list(argv))
    root = args.project.expanduser().resolve()
    bundle = load_task(root, args.task)
    if not bundle.manifest.comparison:
        sys.stderr.write("vibesys compare: the task declares no [[comparison]]\n")
        return 2
    project = Project.open(root)
    settings = load_slurm_operator_settings(args.slurm_config.expanduser())
    scratch_root = project.state.comparison_directory() / "scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
    clock = SystemClock()
    try:
        host = ComparisonHost(
            clock=clock,
            sleeper=clock,
            blocking=ThreadBlockingRunner(),
            repository=CliGitRepository(
                root, git=Git(OsProcesses()), faults=NullGitTrackerEvents()
            ),
            scratch=scratch,
            processes=OsProcesses(),
        )
        reports = asyncio.run(
            measure_comparisons(
                project,
                bundle,
                settings,
                host,
                only=tuple(args.only),
                force=args.force,
                samples=args.samples,
            )
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if args.json:
        document = [
            {
                "name": r.name,
                "label": r.label,
                "stored": r.cached,
                "reading": r.reading.model_dump(mode="json"),
            }
            for r in reports
        ]
        print(json.dumps(document, indent=2), file=out)
    else:
        for report in reports:
            print(describe(report), file=out)
    return 0 if all(isinstance(r.reading, ComparisonMeasured) for r in reports) else 1
