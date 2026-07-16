"""Score validity gates for the KV-store CPU-efficiency metric."""

from __future__ import annotations

import math
from typing import Any


def valid_number(value: Any, *, positive: bool = False) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else True)
    )


def evaluate_validity(
    *,
    throughput: float | None,
    cpu_per_op: float | None,
    rounds: list[dict[str, Any]],
    read_p99_ms: float | None,
    update_p99_ms: float | None,
    saturation_gain_pct: float | None,
    min_throughput: float,
    max_read_p99_ms: float,
    max_update_p99_ms: float,
    max_saturation_gain_pct: float,
) -> tuple[dict[str, bool], list[str]]:
    checks = {
        "throughput_floor": valid_number(throughput) and throughput >= min_throughput,
        "read_p99": valid_number(read_p99_ms) and read_p99_ms < max_read_p99_ms,
        "update_p99": valid_number(update_p99_ms) and update_p99_ms < max_update_p99_ms,
        "score_available": valid_number(cpu_per_op, positive=True),
        "cpu_samples": bool(rounds) and all(round_["cpu_valid"] for round_ in rounds),
        "saturation": valid_number(saturation_gain_pct)
        and abs(saturation_gain_pct) <= max_saturation_gain_pct,
    }
    labels = {
        "throughput_floor": f"throughput must be >= {min_throughput:.1f} ops/sec",
        "read_p99": f"READ p99 must be < {max_read_p99_ms:.3f} ms",
        "update_p99": f"UPDATE p99 must be < {max_update_p99_ms:.3f} ms",
        "score_available": "server CPU/op must be finite and positive",
        "cpu_samples": "every scored repeat must have stable complete CPU accounting",
        "saturation": (
            f"higher-load throughput change must be within ±{max_saturation_gain_pct:.1f}%"
        ),
    }
    reasons = [labels[name] for name, passed in checks.items() if not passed]
    return checks, reasons
