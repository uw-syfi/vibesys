"""A trace of the system under load: ``main(trace=Trace(...))`` and ``bench.py trace``.

The capture and the analysis are stitch's (the trace analyser, found like the core's binaries:
``core.find_binary("stitch")``). With the system deployed in its namespace, ``run_trace``

1. instruments it: ``stitch capture otel-k8s start`` deploys an OpenTelemetry collector into the
   namespace and points the traced Deployments at it, waiting for their rollouts;
2. runs the task's trace body, whose phases are the load (each phase's window is on loadgen's
   clock, from its summary);
3. collects the spans of the phases' windows (``stitch capture otel-k8s collect``), ingests them
   into one graph (``stitch ingest``), compresses the raw spans, and renders the reports
   (``stitch render``).

Every stitch command, its output and its exit status go to ``stitch.log``. A step that fails
keeps what the earlier ones wrote and ends the trace with the reason, which ``README.txt`` holds
too; the system is torn down by the deploy layer on every path.
"""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys_evaluator._private.results import finite

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from typing import Any

    from vibesys_evaluator.microservices._private.run import PhaseWindow, Run
    from vibesys_evaluator.microservices.k8s import App

#: The reports' directory in the sample's artifacts.
TRACE_DIR = "trace"
#: The raw capture (``otel.jsonl``, gzipped once ingested, and stitch's ``manifest.json``).
CAPTURE_DIR = "capture"
#: The ingested span graph.
GRAPH_FILE = "graph.json"
#: What ``stitch capture otel-k8s start`` printed: the collector and the traced Deployments.
START_FILE = "start.json"
#: Every stitch command, its output and its exit status.
LOG_FILE = "stitch.log"
#: stitch's guide to the reports, or why the trace is incomplete.
README_FILE = "README.txt"
#: The raw spans stitch's collect writes into the capture directory.
SPANS_FILE = "otel.jsonl"
#: How much of the time around each phase's window is collected, each side.
WINDOW_PAD_NANOS = 1_000_000_000
#: The most a stitch command other than ``start`` may take (``start`` waits for rollouts).
STITCH_TIMEOUT_SECS = 900.0
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@dataclass(frozen=True, kw_only=True)
class Trace:
    """The task's trace (``bench.py trace``), for ``main(trace=...)``: per-hop latency evidence.

    It deploys the system as a benchmark sample does, instruments ``deployments`` with
    OpenTelemetry (a collector in the sample's namespace; each Deployment's ``OTEL_*`` and
    ``JAEGER_*`` environment pointed at it, sampling ``sampler_ratio`` of the traces), runs
    ``body`` on a ``Run`` (load phases, as a benchmark function does; a short one is enough),
    collects the spans of the phases' windows ``settle_secs`` after the last, and writes stitch's
    reports into ``trace/`` of the artifacts: the call tree, the critical path, a waterfall and
    each hop's latency. Nothing is scored: ``run.report`` is refused, and a phase that is not a
    measurement is logged and kept. The task's application must export spans through the
    OpenTelemetry or Jaeger environment it is given.

    ``deployments`` are the Deployments to instrument; None: the ``k8s.App``'s ``restartable``
    ones (never the data tier, whose pods restart when their environment changes).
    """

    body: Callable[[Run], None]
    sampler_ratio: float = 1.0
    deployments: Sequence[str] | None = None
    settle_secs: float = 5.0

    def __post_init__(self) -> None:
        """Check the fields."""
        if not callable(self.body):
            msg = f"Trace body is a function of the Run, got {self.body!r}"
            raise TypeError(msg)
        ratio = self.sampler_ratio
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
            msg = f"Trace sampler_ratio must be a number in (0, 1], got {ratio!r}"
            raise ValueError(msg)
        settle = self.settle_secs
        if isinstance(settle, bool) or not isinstance(settle, (int, float)) or not finite(settle):
            msg = f"Trace settle_secs must be a finite number >= 0, got {settle!r}"
            raise ValueError(msg)
        if settle < 0:
            msg = f"Trace settle_secs must be a finite number >= 0, got {settle!r}"
            raise ValueError(msg)
        if self.deployments is not None:
            if isinstance(self.deployments, str) or not self.deployments:
                msg = "Trace deployments is a non-empty sequence of Deployment names, or None"
                raise ValueError(msg)
            for name in self.deployments:
                if not isinstance(name, str) or not _NAME.match(name):
                    msg = f"Trace deployments: {name!r} is not a Deployment's name"
                    raise ValueError(msg)
            object.__setattr__(self, "deployments", tuple(self.deployments))


def traced_deployments(trace: Trace, app: App[Any]) -> tuple[str, ...]:
    """The Deployments a trace instruments: its own, else the App's ``restartable``.

    Raises:
        ValueError: Neither names one.
    """
    names = tuple(trace.deployments) if trace.deployments is not None else tuple(app.restartable)
    if not names:
        msg = (
            "the trace has no Deployment to instrument: pass Trace(deployments=[...]) or give "
            "the k8s.App its application tier as restartable=[...]"
        )
        raise ValueError(msg)
    return names


class StitchError(Exception):
    """A stitch command did not succeed; the message names it and carries its output's tail."""


@dataclass(frozen=True)
class Stitch:
    """Runs stitch commands, each logged with its output and exit status to ``log_file``."""

    binary: str
    log_file: Path

    def run(self, args: Sequence[str], *, timeout_secs: float = STITCH_TIMEOUT_SECS) -> str:
        """Run ``stitch ARGS``; return its standard output.

        Raises:
            StitchError: It exited non-zero, ran past ``timeout_secs`` or could not start.
        """
        argv = [self.binary, *args]
        command = " ".join(["stitch", *args])
        with self.log_file.open("a") as log:
            log.write(f"$ {command}\n")
            log.flush()
            try:
                done = subprocess.run(
                    argv, capture_output=True, text=True, timeout=timeout_secs, check=False
                )
            except subprocess.TimeoutExpired as error:
                log.write(f"timed out after {timeout_secs:g} s\n\n")
                msg = f"{command} ran past {timeout_secs:g} s"
                raise StitchError(msg) from error
            except OSError as error:
                log.write(f"could not start: {error}\n\n")
                msg = f"cannot run stitch at {self.binary!r}: {error}"
                raise StitchError(msg) from error
            if done.stdout:
                log.write(f"--- stdout\n{done.stdout.rstrip()}\n")
            if done.stderr:
                log.write(f"--- stderr\n{done.stderr.rstrip()}\n")
            log.write(f"exit {done.returncode}\n\n")
        if done.returncode != 0:
            tail = (done.stderr or done.stdout).strip()[-1500:]
            msg = f"{command} exited {done.returncode}: {tail}"
            raise StitchError(msg)
        return done.stdout


def start_args(
    *, namespace: str, deployments: Sequence[str], sampler_ratio: float, timeout_secs: float
) -> list[str]:
    """``stitch capture otel-k8s start``'s arguments."""
    args = ["capture", "otel-k8s", "start", "--namespace", namespace]
    for name in deployments:
        args += ["--deployment", name]
    return [
        *args,
        "--sampler-ratio",
        repr(float(sampler_ratio)),
        "--timeout-seconds",
        str(max(1, round(timeout_secs))),
    ]


def collect_args(
    *, namespace: str, out: Path, settle_secs: float, windows: Sequence[PhaseWindow]
) -> list[str]:
    """``stitch capture otel-k8s collect``'s arguments: each window padded, labelled by its phase."""
    args = [
        "capture",
        "otel-k8s",
        "collect",
        "--namespace",
        namespace,
        "--out",
        str(out),
        "--settle-seconds",
        str(round(settle_secs)),
    ]
    for window in windows:
        start = max(0, window.start_unix_nanos - WINDOW_PAD_NANOS)
        end = window.end_unix_nanos + WINDOW_PAD_NANOS
        args += ["--window", f"{start}:{end}", "--label", window.name]
    return args


def run_trace(
    trace: Trace,
    *,
    stitch: Stitch,
    namespace: str,
    deployments: Sequence[str],
    out: Path,
    rollout_secs: float,
    body: Callable[[], None],
    windows: Callable[[], Sequence[PhaseWindow]],
    log: Callable[[str], None],
) -> str | None:
    """Instrument, run ``body``, collect, ingest and render into ``out``; None, or what failed.

    ``out`` exists and holds ``stitch.log`` (``stitch.log_file``). ``windows`` gives the
    phases' windows once ``body`` has returned or raised. An exception of ``body`` (other than
    a signal's) is a failure, but the spans of the phases it ran are still collected.
    """
    problems: list[str] = []
    capture = out / CAPTURE_DIR
    graph = out / GRAPH_FILE
    try:
        log(f"trace: instrumenting {', '.join(deployments)} in {namespace}")
        started = stitch.run(
            start_args(
                namespace=namespace,
                deployments=deployments,
                sampler_ratio=trace.sampler_ratio,
                timeout_secs=rollout_secs,
            ),
            timeout_secs=rollout_secs + STITCH_TIMEOUT_SECS,
        )
        (out / START_FILE).write_text(started)
    except StitchError as error:
        return incomplete(out, [f"instrumenting the system failed: {error}"], log)
    try:
        body()
    except Exception as error:
        problems.append(f"the trace body failed: {type(error).__name__}: {error}")
        log(problems[-1])
    phases = list(windows())
    if not phases:
        log("trace: no phase left a window; collecting every span since the start")
    try:
        log(f"trace: collecting the spans of {len(phases)} phase window(s)")
        stitch.run(
            collect_args(
                namespace=namespace, out=capture, settle_secs=trace.settle_secs, windows=phases
            ),
            timeout_secs=trace.settle_secs + STITCH_TIMEOUT_SECS,
        )
        stitch.run(["ingest", str(capture / "manifest.json"), "--out", str(graph)])
        _compress(capture / SPANS_FILE)
        stitch.run(["render", str(graph), "--out", str(out)])
    except StitchError as error:
        problems.append(str(error))
    if problems:
        return incomplete(out, problems, log)
    log(f"trace: the reports are in {out}")
    return None


def _compress(spans: Path) -> None:
    """Replace the raw spans with ``otel.jsonl.gz`` (the graph holds what the reports need)."""
    if not spans.is_file():
        return
    packed = spans.with_name(spans.name + ".gz")
    with spans.open("rb") as raw, gzip.open(packed, "wb") as out:
        shutil.copyfileobj(raw, out)
    spans.unlink()


def incomplete(out: Path, problems: Sequence[str], log: Callable[[str], None]) -> str:
    """Put why the trace is incomplete at the top of README.txt; return it as the failure."""
    readme = out / README_FILE
    previous = readme.read_text() if readme.is_file() else ""
    lines = [
        "THE TRACE IS INCOMPLETE",
        "",
        *[f"- {problem}" for problem in problems],
        "",
        f"Every stitch command and its output are in {LOG_FILE}; whatever the steps before the "
        "failure wrote is kept here.",
    ]
    if (out / CAPTURE_DIR / (SPANS_FILE + ".gz")).is_file():
        lines.append(
            f"The raw spans are {CAPTURE_DIR}/{SPANS_FILE}.gz: gunzip it before "
            f"`stitch ingest {CAPTURE_DIR}/manifest.json`."
        )
    text = "\n".join(lines) + "\n"
    readme.write_text(text + ("\n" + previous if previous else ""))
    for problem in problems:
        log(f"trace: {problem}")
    return "; ".join(problems)
