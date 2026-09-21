"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from vs_agent.callbacks import AgentLogger
from vs_agent.drivers.mock import MockDriver, MockDriverError, ScriptedPlaybook
from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.stub_runner import StubAgentClient

__all__ = [
    "AgentLogger",
    "FakeAgentClient",
    "FakeInvocation",
    "MockDriver",
    "MockDriverError",
    "ScriptedPlaybook",
    "StubAgentClient",
]
