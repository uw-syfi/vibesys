"""Raw Fake turns preserve configured known and unknown usage in the journal input."""

from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentUsage,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentClient
from vs_sim.api.testing import SimThreads


@pytest.mark.regression
@example(usage=AgentUsage(input_tokens=10, output_tokens=2, total_cost_usd=0.1), keyed=True)
@given(
    usage=st.builds(
        AgentUsage,
        **{
            field: st.one_of(st.none(), st.integers(min_value=0, max_value=1000))
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
                "duration_ms",
                "reasoning_output_tokens",
            )
        },
        total_cost_usd=st.one_of(
            st.none(), st.floats(min_value=0, max_value=1000, allow_nan=False)
        ),
    ),
    keyed=st.booleans(),
)
def test_raw_turn_preserves_every_reported_usage_field(usage: AgentUsage, *, keyed: bool) -> None:
    client = FakeAgentClient(threads=SimThreads()).set_text("worker", "done").set_usage(usage)
    result = client.run(
        session_spec=AgentSessionSpec(
            role="worker",
            provider="fake",
            workspace=Path("workspace"),
            policy=AgentExecutionPolicy(require_enforcement=False),
        ),
        turn=AgentTurnRequest(message="Work.", invocation_id="invocation-1"),
        session_key=AgentSessionKey(SessionScope.CHAT, "thread-1") if keyed else None,
    )
    assert result.usage == usage
