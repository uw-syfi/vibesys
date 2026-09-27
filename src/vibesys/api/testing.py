"""The owned fake/test-double surface for VibeSys orchestration.

Tests import doubles from here rather than reaching into core's internal
modules directly. ``FakeGateExecutor`` is its double for ``GateExecutor``
(``vibesys.orchestration.gates``), the seam ``RunContext.open`` accepts as
``gate_executor``. Agent and sandbox doubles live in their owning libraries'
``api.testing`` modules.
"""

from __future__ import annotations

from vibesys.orchestration.fake_gates import (
    DEFAULT_ACCURACY_RESULT,
    DEFAULT_BENCHMARK_RESULT,
    FakeAccuracyCall,
    FakeBenchmarkCall,
    FakeGateExecutor,
)

__all__ = [
    "DEFAULT_ACCURACY_RESULT",
    "DEFAULT_BENCHMARK_RESULT",
    "FakeAccuracyCall",
    "FakeBenchmarkCall",
    "FakeGateExecutor",
]
