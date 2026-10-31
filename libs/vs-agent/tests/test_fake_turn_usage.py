"""Raw Fake turns preserve configured known and unknown usage in the journal input."""

import json
import tempfile
from pathlib import Path
from typing import NamedTuple

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


@pytest.mark.regression
@pytest.mark.parametrize("keyed", [False, True])
def test_an_unconfigured_turn_reports_unknown_usage(*, keyed: bool) -> None:
    """A turn with no configured usage reports every count unknown."""
    client = FakeAgentClient(threads=SimThreads()).set_text("worker", "done")
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
    assert result.usage == AgentUsage()


class _Turn(NamedTuple):
    method: str
    key: int
    fails: bool


_KEYS = tuple(AgentSessionKey(SessionScope.CHAT, f"thread-{index}") for index in range(3))
_TURNS = st.builds(
    _Turn,
    method=st.sampled_from(("invoke_text", "raw")),
    key=st.integers(min_value=0, max_value=len(_KEYS) - 1),
    fails=st.booleans(),
)


@pytest.mark.regression
@example(turns=[_Turn("invoke_text", 0, fails=False)])
@example(turns=[_Turn("raw", 1, fails=False), _Turn("raw", 1, fails=True)])
@given(turns=st.lists(_TURNS, min_size=1, max_size=8))
def test_a_usage_row_names_the_conversation_its_turn_ran_in(turns: list[_Turn]) -> None:
    """The row of a turn holds the id the turn returned, or the one on record when it failed.

    The real client appends the provider's session id to ``usage.jsonl`` with every row; a Fake
    that returned an id and wrote none made a reader joining rows to sessions look correct
    against the Fake and find nothing against the product.
    """
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        client = FakeAgentClient(
            session_reuse=True, log_dir=log_dir, threads=SimThreads()
        ).set_text("worker", "done")
        expected: list[str | None] = []
        for index, turn in enumerate(turns):
            key = _KEYS[turn.key]
            on_record = client.provider_session_id(key)
            if turn.fails:
                client.fail("worker", RuntimeError("provider died"), times=1)
            try:
                _run(client, turn.method, key, index)
            except RuntimeError:
                assert turn.fails
                expected.append(on_record)
            else:
                assert not turn.fails
                expected.append(client.last_turn_provider_session_id(key))
        rows = [
            json.loads(line)
            for line in (log_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()
        ]
    assert [row["provider_session_id"] for row in rows] == expected


def _run(client: FakeAgentClient, method: str, key: AgentSessionKey, index: int) -> None:
    if method == "invoke_text":
        client.invoke_text(
            kind="worker",
            workspace=Path("workspace"),
            system_prompt="system",
            user_prompt="Work.",
            round_label=f"turn {index}",
            reuse_session=True,
            session_key=key,
        )
        return
    client.run(
        session_spec=AgentSessionSpec(
            role="worker",
            provider="fake",
            workspace=Path("workspace"),
            policy=AgentExecutionPolicy(require_enforcement=False),
        ),
        turn=AgentTurnRequest(
            message="Work.", invocation_id=f"invocation-{index}", label=f"turn {index}"
        ),
        session_key=key,
    )
