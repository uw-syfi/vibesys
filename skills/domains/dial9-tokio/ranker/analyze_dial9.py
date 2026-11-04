"""Rank dial9 trace components into profile result protocol v1.

This is the attribution input to vibesys' profile-guided policy, for a tokio
engine (RisingWave) instrumented with dial9. It answers one question: inside
the running engine, which component burns the most on-CPU time in the
steady-state window. It does NOT score the engine; the scored metric stays the
workload driver's `throughput_rows_per_s`.

Division of labor: `extract/dial9_extract.js` owns everything that needs the
dial9 toolkit (parsing segments, reconstructing poll spans, claiming on-CPU
nanoseconds to the innermost covering span, running upstream's setup
diagnostics) and emits a stable JSON report. This module owns *policy*: which
diagnostics invalidate a ranking, how cost and share are defined, and what
evidence an agent needs to read a component correctly. Keeping the toolkit
behind the extractor means this file has no Node or dial9 knowledge in it.

Cost model, the decision that determines whether the ranking is honest:

    cost  = in-window on-CPU poll seconds claimed by the component
    share = that over total in-window on-CPU poll time

Scheduler delay, wake-to-poll delay, off-CPU time, queue depth and worker
utilization never enter `cost`. Mixing on-CPU and off-CPU time into one number
makes `share` meaningless and double-counts a task that is both slow and
starved. They go into `evidence` instead, so a *starved* operator reads as low
cost with a large delay note, which is the correct diagnosis and not something
a CPU-only profiler can produce.

Shares sum to at most 1, not to exactly 1. The shortfall is poll time no span
covered, reported as `unattributed` in the stdout summary.

The window JSON is the workload driver's own output. Its `window` object
reports either `startNs`/`endNs` in the trace monotonic timebase or
`startUnixNs`/`endUnixNs` in wall clock, never both; see `WindowBasis`.

Exit 0 = wrote a ranked attribution; 2 = setup/tool error (no verdict). A
degraded ranking is never emitted: a wrong answer about where time goes is
worse than no answer, because the loop would spend rounds attacking the wrong
component.

Usage (the profile-guided outer loop appends --vs-output itself):
  python3 dial9/analyze_dial9.py \
      --traces /tmp/dial9-traces \
      --window logs/workload.json \
      --vs-output logs/attribution.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_EXTRACTOR = os.path.join(_HERE, "extract", "dial9_extract.js")

# The toolkit decodes one segment at a time and `JSON.parse`s its whole cache
# into memory; a single segment of a real run reaches ~1 GB of JSON, which
# overflows Node's default old-space. The extractor holds one decoded segment
# plus the running accumulators, so this bound is generous, not proportional to
# the number of segments. Overridable via the NODE_OPTIONS the operator sets.
_NODE_MAX_OLD_SPACE_MB = 16384

# The extractor's report schema. Bumped together with `SCHEMA` there, so an
# upgraded extractor cannot be read with stale field assumptions.
SUPPORTED_SCHEMA = 1

COST_UNIT = "poll_seconds"

# Spans shorter than this contribute to their component's total but are not
# worth naming individually in evidence.
_NS_PER_S = 1_000_000_000


class WindowBasis(StrEnum):
    """Which timebase a reported steady-state window is expressed in.

    dial9 event timestamps are monotonic (nanoseconds since boot), so a window
    measured with the wall clock has to be converted through the trace's clock
    sync anchors before it can clip anything. The conversion lives in the
    extractor, which is the only side that has the anchors; this side only has
    to know which of the two a driver handed it, because the mistake it guards
    against (a window in the wrong timebase) does not look like an error, it
    looks like a ranking of the wrong slice of the run.
    """

    MONOTONIC = "monotonic"
    UNIX = "unix"


# The workload-driver key pair, and the extractor flag pair, for each basis.
# One table per direction rather than a branch at each use, so adding a basis
# cannot leave half the code behind.
_WINDOW_FORMS: dict[WindowBasis, tuple[str, str]] = {
    WindowBasis.MONOTONIC: ("startNs", "endNs"),
    WindowBasis.UNIX: ("startUnixNs", "endUnixNs"),
}
_WINDOW_FLAGS: dict[WindowBasis, tuple[str, str]] = {
    WindowBasis.MONOTONIC: ("--start", "--end"),
    WindowBasis.UNIX: ("--start-unix-ns", "--end-unix-ns"),
}
_WINDOW_KEYS = tuple(key for keys in _WINDOW_FORMS.values() for key in keys)


@dataclass(frozen=True)
class Window:
    """A steady-state window and the timebase it was measured in."""

    start: int
    end: int
    basis: WindowBasis


class Check(StrEnum):
    """Setup diagnostics the pinned dial9 toolkit can report."""

    MISSING_FRAME_POINTERS = "missing-frame-pointers"
    MISSING_WAKE_EVENTS = "missing-wake-events"
    MISSING_DEBUG_SYMBOLS = "missing-debug-symbols"
    NO_SCHEDULING_EVENTS = "no-scheduling-events"
    NO_TOKIO_UNSTABLE = "no-tokio-unstable"
    NO_TASK_POLLS = "no-task-polls"


# Which diagnostics invalidate the ranking outright.
#
# Severity alone is not the criterion, which is why this mapping is explicit.
# Upstream rates `no-tokio-unstable` a warning, but without `--cfg
# tokio_unstable` poll events cover only dial9's own spawns, so every cost
# number is computed over a biased subset and the ranking is wrong rather than
# merely incomplete. Conversely `missing-debug-symbols` only costs us readable
# frame names in evidence and leaves cost untouched.
FATAL_CHECKS: frozenset[Check] = frozenset(
    {
        Check.MISSING_FRAME_POINTERS,
        Check.NO_TOKIO_UNSTABLE,
        Check.NO_TASK_POLLS,
    }
)


class SetupError(RuntimeError):
    """The traces cannot support a trustworthy ranking."""


def _fatal_diagnostics(diagnostics: list[dict[str, Any]]) -> list[str]:
    """Return messages for diagnostics that invalidate the ranking.

    Unknown check names are treated as fatal. The toolkit is pinned, so a name
    this module does not classify means the extractor was upgraded without
    revisiting this policy, and a new diagnostic is more likely to describe a
    measurement flaw than a cosmetic one. Failing closed turns that into a
    loud error instead of a silently mis-ranked report.
    """
    fatal = []
    for finding in diagnostics:
        name = finding.get("check", "")
        message = finding.get("message", "")
        try:
            check = Check(name)
        except ValueError:
            fatal.append(f"unclassified dial9 diagnostic '{name}': {message}")
            continue
        if check in FATAL_CHECKS:
            fatal.append(f"[{check}] {message}")
    return fatal


def validate(report: dict[str, Any]) -> None:
    """Reject reports that cannot produce a trustworthy ranking.

    Raises `SetupError` naming every reason, so one run surfaces all of them
    rather than making the operator fix build flags one round at a time.
    """
    schema = report.get("schema")
    if schema != SUPPORTED_SCHEMA:
        raise SetupError(
            f"extractor report schema {schema!r} is not the supported schema "
            f"{SUPPORTED_SCHEMA}; the extractor and this analyzer are out of step"
        )

    problems = _fatal_diagnostics(report.get("diagnostics", []))
    trace = report.get("trace", {})
    totals = report.get("totals", {})

    if not trace.get("hasFullTaskCoverage", False):
        problems.append(
            "trace covers only tasks spawned through dial9's own helpers "
            "(tokio.poll_coverage=dial9-spawns-only); cost would be computed over a "
            "biased subset of tasks"
        )
    if not trace.get("hasSpans", False):
        problems.append(
            "trace carries no spans, so no component can be named; attach "
            "Dial9TracingLayer so the engine's per-actor tracing spans are recorded"
        )
    if totals.get("pollOnCpuNs", 0) <= 0:
        problems.append("no in-window on-CPU poll time; the window excludes all activity")
    if totals.get("unresolvedSpanCount", 0) > 0:
        problems.append(
            f"{totals['unresolvedSpanCount']} spans could not be resolved to a task, so "
            "their on-CPU time is claimed against a shared pool and may suppress a real "
            "component's cost"
        )
    if not report.get("components"):
        problems.append("no component claimed any in-window on-CPU time")

    if problems:
        raise SetupError("; ".join(problems))


def _run_context(report: dict[str, Any]) -> str:
    """One compact line of run-level facts that change how any component reads.

    Deliberately repeated on every component rather than hoisted: the protocol
    carries per-component `evidence` and nothing else, and a reader deciding
    whether a component is slow or merely starved needs the scheduler-delay and
    unattributed figures next to that component's own numbers.
    """
    totals = report["totals"]
    poll = totals["pollOnCpuNs"] or 1
    sched = report.get("schedDelay", {})
    workers = report.get("workers", [])
    utilization = (
        sum(w["utilization"] for w in workers) / len(workers) if workers else 0.0
    )
    parts = [
        f"unattributed {totals['unattributedOnCpuNs'] / poll:.1%} of poll time",
        f"mean worker utilization {utilization:.1%}",
    ]
    if sched.get("count"):
        parts.append(f"sched delay p50 {sched['p50Ns'] / 1e6:.2f}ms p99 {sched['p99Ns'] / 1e6:.2f}ms")
    queue = report.get("queueDepth", {})
    if queue.get("samples"):
        parts.append(f"queue depth max {queue['max']} avg {queue['avg']:.1f}")
    return "run: " + ", ".join(parts)


def _evidence(component: dict[str, Any], context: str) -> list[str]:
    """Per-component facts an agent needs to read the number correctly."""
    self_ns = component["selfOnCpuNs"]
    cumulative = component["onCpuNs"]
    wall = component["wallNs"]
    lines = [
        f"{self_ns / _NS_PER_S:.3f}s on-CPU self time across {component['count']} spans "
        f"on {component['taskCount']} tasks",
    ]
    # A wrapper span whose children hold most of the time: attacking the
    # wrapper itself would be wasted work, so say so explicitly.
    if cumulative > self_ns:
        lines.append(
            f"{cumulative / _NS_PER_S:.3f}s including nested spans, so "
            f"{1 - self_ns / cumulative:.0%} of its time is inside instrumented children"
        )
    # Wall clock far above on-CPU time means the span is blocked or starved
    # rather than computing. This is the signal a CPU-only profiler cannot see.
    if wall > 0 and cumulative > 0 and wall > 2 * cumulative:
        lines.append(
            f"{wall / _NS_PER_S:.3f}s wall clock versus {cumulative / _NS_PER_S:.3f}s on-CPU: "
            f"spends {1 - cumulative / wall:.0%} of its elapsed time off-CPU (blocked or starved)"
        )
    lines.append(context)
    return lines


def build_result(report: dict[str, Any]) -> dict[str, Any]:
    """Convert a validated extractor report into profile result protocol v1.

    Components keep the extractor's ordering, which is descending self time
    with name as the tiebreak, so the ranking is total and stable.
    """
    context = _run_context(report)
    return {
        "version": 1,
        "cost_unit": COST_UNIT,
        "components": [
            {
                "name": component["name"],
                # Floats, not ints, because the protocol validates in pydantic
                # strict mode.
                "cost": float(component["selfOnCpuNs"]) / _NS_PER_S,
                "share": float(component["share"]),
                "evidence": _evidence(component, context),
            }
            for component in report["components"]
        ],
    }


def _window(path: str | None) -> Window | None:
    """Read the steady-state window the workload driver measured.

    Clipping to it is what keeps startup and ramp out of the ranking. A driver
    output without a window is an error rather than a full-trace fallback: the
    ranking would silently include warmup and no longer match the benchmark
    metric measured over the same run.

    Which timebase the driver reports in is its choice, not a guess made here.
    A driver that reads the trace timebase reports `startNs`/`endNs`; one that
    only has `time.clock_gettime(CLOCK_REALTIME)` reports
    `startUnixNs`/`endUnixNs` and the extractor converts via the trace's clock
    sync anchors. Supplying both is an error: the two would have to agree, and
    if they did not, picking either silently ranks the wrong slice.
    """
    if path is None:
        return None
    with open(path) as handle:
        payload = json.load(handle)
    window = payload.get("window")
    if not isinstance(window, dict):
        raise SetupError(
            f"{path} has no window object; the workload driver must report the "
            "steady-state window the traces are clipped to"
        )
    unknown = sorted(set(window) - set(_WINDOW_KEYS))
    if unknown:
        raise SetupError(f"{path} window has unknown key(s): {', '.join(unknown)}")
    forms = [
        (basis, start_key, end_key)
        for basis, (start_key, end_key) in _WINDOW_FORMS.items()
        if start_key in window or end_key in window
    ]
    if not forms:
        raise SetupError(
            f"{path} window must carry either startNs/endNs (trace monotonic timebase) "
            "or startUnixNs/endUnixNs (wall clock)"
        )
    if len(forms) > 1:
        raise SetupError(
            f"{path} window mixes timebases; report either startNs/endNs or "
            "startUnixNs/endUnixNs, not both"
        )
    basis, start_key, end_key = forms[0]
    missing = [key for key in (start_key, end_key) if key not in window]
    if missing:
        raise SetupError(f"{path} window is missing {', '.join(missing)}")
    start, end = int(window[start_key]), int(window[end_key])
    if end <= start:
        raise SetupError(f"{path} window {end_key} must be greater than {start_key}")
    return Window(start=start, end=end, basis=basis)


def extract(
    traces: str, *, extractor: str, toolkit: str | None, window: Window | None
) -> dict[str, Any]:
    """Run the Node extractor and return its report.

    The extractor writes the report to a file because the dial9 toolkit's own
    `diagnoseSetup` prints a human-readable report (with its fix instructions)
    to stdout. That text is forwarded to this process' stdout, where it lands
    in the run log next to the ranking.
    """
    if not os.path.exists(extractor):
        raise SetupError(f"extractor not found: {extractor}")
    with tempfile.TemporaryDirectory() as scratch:
        report_path = os.path.join(scratch, "report.json")
        command = [
            "node", f"--max-old-space-size={_NODE_MAX_OLD_SPACE_MB}",
            extractor, traces, "--out", report_path,
        ]
        if toolkit:
            command += ["--toolkit", toolkit]
        if window is not None:
            start_flag, end_flag = _WINDOW_FLAGS[window.basis]
            command += [start_flag, str(window.start), end_flag, str(window.end)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        sys.stdout.write(completed.stdout)
        if completed.returncode != 0:
            raise SetupError(
                f"extractor failed (exit {completed.returncode}): {completed.stderr.strip()}"
            )
        with open(report_path) as handle:
            return json.load(handle)


def _window_note(report: dict[str, Any]) -> str:
    """One line describing what the ranking was clipped to.

    Printed because the window is the single input that most changes the answer
    while leaving it looking plausible, so it belongs in the run log next to
    the numbers it produced. A converted wall-clock window also reports the
    anchor spread, which bounds how far off its boundaries can be.
    """
    window = report["window"]
    if window["startNs"] is None and window["endNs"] is None:
        return "window: whole trace (startup and ramp included)"
    span = (window["endNs"] - window["startNs"]) / _NS_PER_S
    note = f"window: {span:.3f}s, basis {window['basis']}"
    clock = report.get("clock")
    if window["basis"] == WindowBasis.UNIX and clock:
        note += (
            f", converted from wall clock via {clock['anchorCount']} anchor(s)"
            f" (offset spread {clock['offsetSpreadNs']}ns)"
        )
    return note


def _summarize(report: dict[str, Any], result: dict[str, Any]) -> None:
    """Print the human-readable ranking that lands in the run log."""
    print(_window_note(report))
    print("-" * 72)
    print(f"{'component':<34} {'self s':>9} {'share':>7}  tasks")
    print("-" * 72)
    for component, raw in zip(result["components"], report["components"], strict=True):
        print(
            f"{component['name'][:34]:<34} {component['cost']:>9.3f} "
            f"{component['share']:>6.1%}  {raw['taskCount']}"
        )
    print("-" * 72)
    totals = report["totals"]
    poll = totals["pollOnCpuNs"] or 1
    print(
        f"{'unattributed':<34} {totals['unattributedOnCpuNs'] / _NS_PER_S:>9.3f} "
        f"{totals['unattributedOnCpuNs'] / poll:>6.1%}"
    )
    for finding in report.get("diagnostics", []):
        print(f"note: [{finding['check']}] {finding['message']}")
    if result["components"]:
        top = result["components"][0]
        print(f"Top bottleneck: {top['name']} ({top['share']:.1%} of on-CPU poll time)")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on a written ranking, 2 on setup error."""
    parser = argparse.ArgumentParser(description="Rank dial9 trace components.")
    parser.add_argument("--traces", required=True, help="dial9 trace file or directory")
    parser.add_argument(
        "--window", default=None, help="workload driver JSON carrying the steady-state window"
    )
    parser.add_argument("--vs-output", required=True, help="where to write protocol v1 JSON")
    parser.add_argument("--extractor", default=_DEFAULT_EXTRACTOR, help="path to dial9_extract.js")
    parser.add_argument("--toolkit", default=None, help="dial9 toolkit script directory")
    args = parser.parse_args(argv)

    try:
        report = extract(
            args.traces,
            extractor=args.extractor,
            toolkit=args.toolkit,
            window=_window(args.window),
        )
        validate(report)
        result = build_result(report)
    except SetupError as error:
        print(f"analyze-dial9 ERROR: {error}", file=sys.stderr)
        return 2

    _summarize(report, result)
    os.makedirs(os.path.dirname(os.path.abspath(args.vs_output)), exist_ok=True)
    with open(args.vs_output, "w") as handle:
        json.dump(result, handle, indent=2)
    print(f"  wrote {args.vs_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
