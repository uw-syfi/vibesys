"""The owned fake/test-double surface for ``vs_agent``.

Tests import doubles from here rather than reaching into the library's
internal modules directly.
"""

from __future__ import annotations

from vs_agent.fake_client import FakeAgentClient, FakeInvocation
from vs_agent.fake_response import AgentResponseContext, AgentResponseScenario

__all__ = [
    "AgentResponseContext",
    "AgentResponseScenario",
    "FakeAgentClient",
    "FakeInvocation",
]
