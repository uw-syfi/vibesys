"""The owned fake/test-double surface for VibeSys core.

Tests import doubles from here rather than reaching into core's internal
modules directly. ``FakeComputeBackend`` is core's in-memory double for the
``ComputeBackendImpl`` protocol (``vibesys.backends.base``); ``FakeGateExecutor``
is its double for ``GateExecutor`` (``vibesys.orchestration.gates``), the seam
``RunContext``/``run_orchestration`` accept as ``gate_executor``. The
agent-client double lives in ``vs_agent.api.testing`` and the sandbox double
it composes lives in ``vs_sandbox.api.testing``.
"""

from __future__ import annotations

from vibesys.backends.fake import FakeComputeBackend
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
    "FakeComputeBackend",
    "FakeGateExecutor",
]
