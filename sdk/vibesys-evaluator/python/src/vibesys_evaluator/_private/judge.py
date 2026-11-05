"""Whether a phase's output is a measurement: the saturation guard and the validity rules.

Pure: a ``PhaseOutput`` and the rules in, a ``PhaseResult`` or the reason
out (``docs/spec.md``, "Saturation guard and validity").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys_evaluator._private.hdr import HistogramError, decode
from vibesys_evaluator._private.results import PhaseResult
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

if TYPE_CHECKING:
    from vibesys_evaluator._private.results import Validity
    from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

#: The share of the window's operations that may have waited for the generator.
MAX_GENERATOR_WAITED = 0.01
#: loadgen's CPU over the window, as a share of its quota, above which it may be the bottleneck.
MAX_LOADGEN_CPU = 0.8
#: Open loop: the send lag's p99 may reach this share of the latency p99 ...
MAX_SEND_LAG_SHARE = 0.1
#: ... or this floor, whichever is larger. ``loadgen selftest`` (work package 7) will replace
#: the floor with twice the latency it measures loadgen adding.
SEND_LAG_FLOOR_MS = 1.0
#: Open loop: when at least this share of the window's operations were due while every sender
#: was busy (loadgen's ``saturated``), the send lag's tail is the application holding the
#: senders, which its latency (end - scheduled) already includes: not a reason to fail.
MIN_SATURATED_SHARE = 0.01
_MICROS_PER_MS = 1000.0
_VIOLATIONS_SHOWN = 3


@dataclass(frozen=True, kw_only=True)
class Failed:
    """The phase is not a measurement."""

    reason: str


def _ms(histogram: pb.HdrHistogram | None, percentile: float) -> float:
    if histogram is None:
        return 0.0
    try:
        return decode(histogram.encoded).percentile(percentile) / _MICROS_PER_MS
    except HistogramError as error:
        msg = f"loadgen's summary holds a histogram it cannot have written: {error}"
        raise ValueError(msg) from error


def _errors(summary: pb.PhaseSummary) -> str:
    kinds = ", ".join(
        f"{pb.ErrorKind(kind).name.lower()} {count}"
        for kind, count in sorted(summary.errors_by_kind.items())
        if count
    )
    return f" ({kinds})" if kinds else ""


def _guard(summary: pb.PhaseSummary, p99_ms: float, validity: Validity) -> str | None:
    """The saturation guard and the sustained rate: did loadgen offer the load asked for?"""
    generator = summary.generator
    if generator is not None and generator.waited > MAX_GENERATOR_WAITED * max(
        summary.completed, 1
    ):
        waited_ms = _ms(generator.wait, 99.0)
        return (
            f"the request generator fell behind: {generator.waited} of the window's "
            f"{summary.completed} operations waited for it (more than {MAX_GENERATOR_WAITED:.0%}; "
            f"p99 wait {waited_ms:.2f} ms); it wrote {generator.rate:.0f} ops/s, so the phase "
            "measures the generator, not the application: make build cheaper or give the phase "
            "pod more CPU"
        )
    if summary.loadgen_cpu_fraction > MAX_LOADGEN_CPU:
        return (
            f"loadgen used {summary.loadgen_cpu_fraction:.0%} of its CPU over the window "
            f"(more than {MAX_LOADGEN_CPU:.0%}): it may be the bottleneck, so the phase measures "
            "it, not the application"
        )
    if summary.model != pb.LoadModel.OPEN:
        return None
    lag_ms = _ms(summary.send_lag, 99.0)
    allowed = max(MAX_SEND_LAG_SHARE * p99_ms, SEND_LAG_FLOOR_MS)
    held = summary.saturated >= MIN_SATURATED_SHARE * max(summary.completed, 1)
    if lag_ms > allowed and not held:
        return (
            f"loadgen sent late: send lag p99 {lag_ms:.2f} ms is above {allowed:.2f} ms "
            f"(the larger of {MAX_SEND_LAG_SHARE:.0%} of latency p99 {p99_ms:.2f} ms and "
            f"{SEND_LAG_FLOOR_MS:.1f} ms); the offered load was not what was asked for"
        )
    ratio = summary.offered_rate / summary.target_rate if summary.target_rate > 0 else 0.0
    if ratio < validity.min_sustained_ratio:
        return (
            f"offered {summary.offered_rate:.1f} ops/s of the {summary.target_rate:.1f} asked for "
            f"({ratio:.1%}, below min_sustained_ratio {validity.min_sustained_ratio:.0%}): "
            "the load was not sustained"
        )
    return None


def _rules(
    output: phase_pb.PhaseOutput,
    summary: pb.PhaseSummary,
    validity: Validity,
) -> str | None:
    """The task's validity rules over the window's operations."""
    if summary.completed == 0:
        return "no operation completed in the measurement window"
    rate = (summary.errors + output.invalid) / summary.completed
    if rate > validity.max_error_rate:
        shown = "; ".join(
            f"{v.operation} #{v.index}: {v.message}" for v in output.violations[:_VIOLATIONS_SHOWN]
        )
        return (
            f"error rate {rate:.4%} is above max_error_rate {validity.max_error_rate:.4%}: "
            f"{summary.errors} loadgen errors{_errors(summary)} and {output.invalid} failed checks "
            f"of {output.checked} checked, over {summary.completed} operations"
            + (f"; first failed checks: {shown}" if shown else "")
        )
    completed = {op.operation: op.completed for op in summary.operations}
    short = [
        f"{name} ({completed.get(n, 0)})"
        for n, name in enumerate(summary.operation_names)
        if completed.get(n, 0) < validity.min_ops_per_operation
    ]
    if short:
        return (
            f"operations completed fewer than min_ops_per_operation="
            f"{validity.min_ops_per_operation} times in the window: {', '.join(short)}; "
            "lengthen the window or raise their weight"
        )
    return None


def judge(output: phase_pb.PhaseOutput, *, validity: Validity) -> PhaseResult | Failed:
    """The phase's result, or why it is not one."""
    summary = output.summary
    if summary is None:
        return Failed(reason="the phase output has no loadgen summary")
    p99_ms = _ms(summary.latency, 99.0)
    reason = _guard(summary, p99_ms, validity) or _rules(output, summary, validity)
    if reason is not None:
        return Failed(reason=reason)
    return PhaseResult(
        ops_per_sec=summary.achieved_rate, p50_ms=_ms(summary.latency, 50.0), p99_ms=p99_ms
    )
