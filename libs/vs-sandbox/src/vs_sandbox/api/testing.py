"""The owned fake/test-double surface for ``vs_sandbox``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from vs_sandbox.fake_sandbox import DEFAULT_RESULT, FakeExecution, FakeSandbox

__all__ = [
    "DEFAULT_RESULT",
    "FakeExecution",
    "FakeSandbox",
]
