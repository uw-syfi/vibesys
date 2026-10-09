"""Identity rules of the agent-facing MCP server spec."""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import MCPServerSpec


@given(st.text(), st.text())
def test_runtime_environment_values_do_not_change_session_identity(
    first: str,
    second: str,
) -> None:
    """Ephemeral values may rotate; their names and stable capability values may not."""
    one = MCPServerSpec(
        name="evaluation",
        command="python",
        env=(("role", "implementer"),),
        runtime_env=(("token", first),),
    )
    two = dataclasses.replace(one, runtime_env=(("token", second),))
    assert one == two
    assert repr(one) == repr(two)
    assert one != dataclasses.replace(one, runtime_env=(("other-token", first),))
    assert one != dataclasses.replace(one, env=(("role", "judge"),))


@given(st.text(min_size=1), st.text(), st.text())
def test_runtime_environment_cannot_override_identity(
    key: str,
    first: str,
    second: str,
) -> None:
    with pytest.raises(ValueError, match="overlaps identity environment keys"):
        MCPServerSpec(
            name="evaluation", command="python", env=((key, first),), runtime_env=((key, second),)
        )
