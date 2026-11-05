"""``PhaseResult``: what a load phase (``Run.closed_loop``, ``Run.open_loop``) measured."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class PhaseResult:
    """What one phase measured in its window.

    Attributes:
        ops_per_sec: Operations completed in the window divided by its length.
        p50_ms: The median latency, milliseconds.
        p99_ms: The 99th percentile latency, milliseconds.
    """

    ops_per_sec: float
    p50_ms: float
    p99_ms: float
