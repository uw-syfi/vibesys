"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly. ``ReplayPlaybook`` and ``MockDriverError`` are also
part of the main :mod:`vs_agent.api` surface (the mock driver is a real
``Driver.MOCK``); production code imports them from there, and this module
re-exports them only so tests have one place to import every double from.
"""

from __future__ import annotations

from vs_agent.callbacks import AgentLogger
from vs_agent.drivers.mock import MockDriver, MockDriverError, ReplayPlaybook, ScriptedPlaybook
from vs_agent.stub_runner import StubAgentClient

__all__ = [
    "AgentLogger",
    "MockDriver",
    "MockDriverError",
    "ReplayPlaybook",
    "ScriptedPlaybook",
    "StubAgentClient",
]
