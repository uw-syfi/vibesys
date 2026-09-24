#!/usr/bin/env python3
"""Microbenchmark + paired A/B toolkit — altitude below serving.

Two halves:

1. A library (``time_callable``, ``paired_ab``) for a driver script that
   already has torch loaded: times a callable with device events (works on
   both ROCm and CUDA — ``torch.cuda.Event`` is the same API on both), drops
   warmup, and reports median/p10/p90/spread plus a still-climbing flag. Both
   functions import torch lazily, on first call, so importing this module
   never requires torch.

2. A CLI (``parse`` / ``compare`` / ``verdict``) that judges *recorded*
   ``wall_ms`` samples without needing torch at all — so an agent can record
   timings from any driver (this library, a shell loop, another language's
   benchmark) and get a verdict from this tool alone.

Driver pattern
--------------
A driver prints one line per timed iteration to stdout::

    wall_ms: <float>

``parse <log>`` extracts every such line into ``{"wall_ms": [...]}`` JSON,
which is the input format ``compare`` reads.

JSON schemas
------------
``compare``'s two files (baseline, candidate), each::

    {"wall_ms": [12.3, 12.1, 12.4, ...]}

Two independent (not necessarily interleaved) series — e.g. a full baseline
run, then a full candidate run. Verdict is by median comparison only.

``verdict``'s one file — genuinely interleaved A,B,A,B,... pairs, e.g. as
recorded by ``paired_ab`` or by a driver that alternates the two callables
itself::

    {"pairs": [[12.3, 11.9], [12.1, 11.8], ...]}

Each pair is ``[baseline_ms, candidate_ms]``. Interleaved pairs are the more
decisive comparison (see ``assess_paired``): every pair's sign can be
checked, catching a candidate that wins on some pairs and loses on others
(``sign_disagreement``), which an unpaired median comparison would silently
average away.

Usage:
    python kernel_bench.py parse driver.log > samples.json
    python kernel_bench.py compare baseline.json candidate.json
    python kernel_bench.py verdict pairs.json [--threshold-pct 3.0]
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_WARMUP = 3
DEFAULT_REPEATS = 10
DEFAULT_PAIRS = 7
DEFAULT_WARMUP_PAIRS = 1
DEFAULT_THRESHOLD_PCT = 3.0

# Below this many usable samples/pairs there is nothing to judge.
MIN_SAMPLES_FOR_VERDICT = 2
MIN_PAIRS_FOR_VERDICT = 2

# A rising pair is not a trend: with two noisy samples, half of all steady
# measurements rise. Three is the smallest series a monotonic trend means
# anything for.
MIN_SAMPLES_FOR_TREND = 3

WALL_MS_RE = re.compile(r"wall_ms:\s*([\d.eE+-]+)")


# ---------------------------------------------------------------------------
# Pure decision logic — no torch, importable and testable without a GPU.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimingResult:
    """A summarized timing series: warmup dropped, converged or not."""

    samples_ms: tuple[float, ...]
    warmup_ms: tuple[float, ...]
    median_ms: float | None
    p10_ms: float | None
    p90_ms: float | None
    spread_pct: float | None
    converged: bool
    reason: str  # "converged" | "monotonic_increasing" | "insufficient_samples"

    def to_dict(self) -> dict[str, object]:
        """Render as a JSON-serializable dict."""
        return {
            "samples_ms": list(self.samples_ms),
            "warmup_ms": list(self.warmup_ms),
            "median_ms": self.median_ms,
            "p10_ms": self.p10_ms,
            "p90_ms": self.p90_ms,
            "spread_pct": self.spread_pct,
            "converged": self.converged,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PairedVerdict:
    """The outcome of an A/B comparison, with the evidence behind it.

    ``median_delta_pct`` is ``(median(b) - median(a)) / median(a) * 100``:
    negative means B (the candidate) took less time than A (the baseline),
    i.e. B is faster.
    """

    decisive: bool
    reason: str  # "faster" | "slower" | "within_noise" | "sign_disagreement" | "not_converged"
    median_delta_pct: float | None
    pairs: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    deltas_pct: tuple[float, ...] = field(default_factory=tuple)

    @property
    def candidate_faster(self) -> bool:
        """True iff the comparison decisively favors the candidate (B)."""
        return self.decisive and self.reason == "faster"

    def to_dict(self) -> dict[str, object]:
        """Render as a JSON-serializable dict."""
        return {
            "decisive": self.decisive,
            "reason": self.reason,
            "median_delta_pct": self.median_delta_pct,
            "candidate_faster": self.candidate_faster,
            "pairs": [list(pair) for pair in self.pairs],
            "deltas_pct": list(self.deltas_pct),
        }


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _is_monotonic_increasing(values: Sequence[float]) -> bool:
    """True only for a series long enough for a rise to mean something."""
    return len(values) >= MIN_SAMPLES_FOR_TREND and all(
        b > a for a, b in itertools.pairwise(values)
    )


def summarize_series(samples_ms: Sequence[float], *, warmup: int = 0) -> TimingResult:
    """Drop ``warmup`` samples, then summarize the rest: median/p10/p90/spread.

    Flags a still-climbing (monotonic increasing) remainder as not converged:
    taking its last value would systematically overstate the result.
    """
    values = [float(v) for v in samples_ms]
    warmup = max(int(warmup), 0)
    warm, used = tuple(values[:warmup]), values[warmup:]
    if len(used) < MIN_SAMPLES_FOR_VERDICT:
        return TimingResult(
            samples_ms=tuple(used),
            warmup_ms=warm,
            median_ms=None,
            p10_ms=None,
            p90_ms=None,
            spread_pct=None,
            converged=False,
            reason="insufficient_samples",
        )

    ordered = sorted(used)
    median_ms = statistics.median(ordered)
    p10_ms = _percentile(ordered, 10)
    p90_ms = _percentile(ordered, 90)
    spread_pct = (p90_ms - p10_ms) / median_ms * 100.0 if median_ms else 0.0
    converged = not _is_monotonic_increasing(used)
    reason = "converged" if converged else "monotonic_increasing"
    return TimingResult(
        samples_ms=tuple(used),
        warmup_ms=warm,
        median_ms=median_ms,
        p10_ms=p10_ms,
        p90_ms=p90_ms,
        spread_pct=spread_pct,
        converged=converged,
        reason=reason,
    )


def assess_paired(
    pairs: Sequence[tuple[float, float]],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    min_pairs: int = MIN_PAIRS_FOR_VERDICT,
) -> PairedVerdict:
    """Judge interleaved ``(baseline_ms, candidate_ms)`` pairs."""
    usable = tuple(
        (float(a), float(b))
        for a, b in pairs
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 0 and b > 0
    )
    if len(usable) < max(int(min_pairs), 1):
        return PairedVerdict(
            decisive=False, reason="not_converged", median_delta_pct=None, pairs=usable
        )

    a_series = [a for a, _ in usable]
    b_series = [b for _, b in usable]
    if _is_monotonic_increasing(a_series) or _is_monotonic_increasing(b_series):
        return PairedVerdict(
            decisive=False, reason="not_converged", median_delta_pct=None, pairs=usable
        )

    deltas = tuple(round((b - a) / a * 100.0, 4) for a, b in usable)
    signs = {1 if d > 0 else (-1 if d < 0 else 0) for d in deltas if d != 0}
    med = round(statistics.median(deltas), 4)

    if len(signs) > 1:
        verdict_reason = "sign_disagreement"
    elif med > threshold_pct:
        verdict_reason = "slower"
    elif med < -threshold_pct:
        verdict_reason = "faster"
    else:
        verdict_reason = "within_noise"
    decisive = verdict_reason != "sign_disagreement"
    return PairedVerdict(
        decisive=decisive,
        reason=verdict_reason,
        median_delta_pct=med,
        pairs=usable,
        deltas_pct=deltas,
    )


def assess_unpaired(
    samples_a: Sequence[float],
    samples_b: Sequence[float],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> PairedVerdict:
    """Judge two independent (non-interleaved) recorded series by median.

    Less decisive than ``assess_paired``: with no per-pair sign to check,
    a candidate that wins on some cases and loses on others just averages
    out here instead of surfacing as ``sign_disagreement``.
    """
    a_summary = summarize_series(samples_a)
    b_summary = summarize_series(samples_b)
    if not a_summary.converged or not b_summary.converged:
        return PairedVerdict(decisive=False, reason="not_converged", median_delta_pct=None)
    # `converged` is only ever True alongside a computed median (see summarize_series).
    a_median = a_summary.median_ms if a_summary.median_ms is not None else 0.0
    b_median = b_summary.median_ms if b_summary.median_ms is not None else 0.0
    med = round((b_median - a_median) / a_median * 100.0, 4) if a_median else 0.0
    if med > threshold_pct:
        return PairedVerdict(decisive=True, reason="slower", median_delta_pct=med)
    if med < -threshold_pct:
        return PairedVerdict(decisive=True, reason="faster", median_delta_pct=med)
    return PairedVerdict(decisive=True, reason="within_noise", median_delta_pct=med)


# ---------------------------------------------------------------------------
# Measurement — torch, imported lazily so the CLI never needs it.
# ---------------------------------------------------------------------------


def _time_one(fn: Callable[[], object], *, use_cuda: bool) -> float:
    """Time one call to ``fn`` in milliseconds, synchronized correctly."""
    if use_cuda:
        import torch  # noqa: PLC0415

        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        torch.cuda.synchronize()
        start.record()
        fn()
        end.record()
        torch.cuda.synchronize()
        return start.elapsed_time(end)
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def _use_cuda(device: str | None) -> bool:
    import torch  # noqa: PLC0415

    return device != "cpu" and torch.cuda.is_available()


def time_callable(
    fn: Callable[[], object],
    *,
    warmup: int = DEFAULT_WARMUP,
    repeats: int = DEFAULT_REPEATS,
    device: str | None = None,
) -> TimingResult:
    """Time ``fn`` with torch device events; drop warmup, summarize the rest.

    Works on both ROCm and CUDA torch builds (``torch.cuda.Event`` is the
    same API on both — ROCm's torch build maps ``torch.cuda`` onto HIP).
    Falls back to ``time.perf_counter()`` wall-clock timing when no CUDA/ROCm
    device is available, or when ``device="cpu"`` is requested explicitly.
    """
    use_cuda = _use_cuda(device)
    samples_ms = [
        _time_one(fn, use_cuda=use_cuda) for _ in range(max(int(warmup), 0) + max(int(repeats), 0))
    ]
    return summarize_series(samples_ms, warmup=warmup)


def paired_ab(
    fn_a: Callable[[], object],
    fn_b: Callable[[], object],
    *,
    pairs: int = DEFAULT_PAIRS,
    warmup_pairs: int = DEFAULT_WARMUP_PAIRS,
    device: str | None = None,
) -> PairedVerdict:
    """Interleaved A,B,A,B,... timing, judged by ``assess_paired``.

    Uses ``DEFAULT_THRESHOLD_PCT``. To judge the same measured pairs against
    a different threshold, re-run ``assess_paired(verdict.pairs, threshold_pct=...)``
    rather than re-measuring.
    """
    use_cuda = _use_cuda(device)
    raw_pairs = [
        (_time_one(fn_a, use_cuda=use_cuda), _time_one(fn_b, use_cuda=use_cuda))
        for _ in range(max(int(pairs), 0))
    ]
    used_pairs = raw_pairs[max(int(warmup_pairs), 0) :]
    return assess_paired(used_pairs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_wall_ms(path: str) -> list[float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "wall_ms" not in payload:
        sys.exit(f"{path}: expected a JSON object with a 'wall_ms' list (see `parse`)")
    values = payload["wall_ms"]
    if not isinstance(values, list) or not all(isinstance(v, (int, float)) for v in values):
        sys.exit(f"{path}: 'wall_ms' must be a list of numbers")
    return [float(v) for v in values]


_PAIR_LEN = 2


def _is_valid_pair(item: object) -> bool:
    return (
        isinstance(item, list)
        and len(item) == _PAIR_LEN
        and all(isinstance(v, (int, float)) for v in item)
    )


def _load_pairs(path: str) -> list[tuple[float, float]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "pairs" not in payload:
        sys.exit(f"{path}: expected a JSON object with a 'pairs' list of [baseline, candidate]")
    raw_pairs = payload["pairs"]
    pairs: list[tuple[float, float]] = []
    for i, item in enumerate(raw_pairs):
        if not _is_valid_pair(item):
            sys.exit(f"{path}: pairs[{i}] must be a [baseline_ms, candidate_ms] pair of numbers")
        pairs.append((float(item[0]), float(item[1])))
    return pairs


def _print_verdict(verdict: PairedVerdict, label_a: str, label_b: str) -> None:
    status = "DECISIVE" if verdict.decisive else "INCONCLUSIVE"
    delta = f"{verdict.median_delta_pct:+.2f}%" if verdict.median_delta_pct is not None else "n/a"
    print(f"{status}: {verdict.reason}  (median delta {label_a} -> {label_b}: {delta})")  # noqa: T201
    if verdict.pairs:
        print(f"pairs used: {len(verdict.pairs)}")  # noqa: T201


def cmd_parse(ns: argparse.Namespace) -> None:
    """Extract ``wall_ms: <float>`` lines from a driver log into JSON."""
    text = Path(ns.log).read_text(encoding="utf-8")
    values = [float(m) for m in WALL_MS_RE.findall(text)]
    print(json.dumps({"wall_ms": values}))  # noqa: T201


def cmd_compare(ns: argparse.Namespace) -> None:
    """Unpaired median comparison of two recorded (non-interleaved) sample files."""
    samples_a = _load_wall_ms(ns.file_a)
    samples_b = _load_wall_ms(ns.file_b)
    verdict = assess_unpaired(samples_a, samples_b, threshold_pct=ns.threshold_pct)
    _print_verdict(verdict, ns.file_a, ns.file_b)


def cmd_verdict(ns: argparse.Namespace) -> None:
    """Paired A/B verdict from a file of genuinely interleaved recorded pairs."""
    pairs = _load_pairs(ns.samples)
    verdict = assess_paired(pairs, threshold_pct=ns.threshold_pct)
    _print_verdict(verdict, "baseline", "candidate")


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse subcommand args and dispatch."""
    parser = argparse.ArgumentParser(
        prog="kernel_bench.py",
        description="Microbenchmark + paired A/B toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="extract wall_ms: <float> lines from a driver log")
    p.add_argument("log")
    p.set_defaults(fn=cmd_parse)

    p = sub.add_parser("compare", help="unpaired median comparison of two recorded sample files")
    p.add_argument("file_a")
    p.add_argument("file_b")
    p.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("verdict", help="paired A/B verdict from interleaved recorded pairs")
    p.add_argument("samples")
    p.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    p.set_defaults(fn=cmd_verdict)

    ns = parser.parse_args(argv)
    ns.fn(ns)


if __name__ == "__main__":
    main()
