"""A degenerate or oversized reply ends the turn as an invalid response, never a hang."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_agent.api import (
    MAX_REPLY_BYTES,
    MAX_WHITESPACE_RUN,
    AgentCapabilities,
    AgentClient,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnExecutor,
    AgentTurnRequest,
    ReplyBreach,
    ReplyGuard,
    SessionScope,
    reply_breach,
)
from vs_agent.api.testing import FakeAgentClient, FakeProvider, assistant_text
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path


_PADDING = st.sampled_from([" ", "\n", " \n\n", "\t", "\r\n"])
_FILLER = st.sampled_from(["x", "ab", '"', "{"])


def _turn(executor: AgentTurnExecutor, tmp_path: Path) -> str:
    spec = AgentSessionSpec(
        role="worker",
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    request = AgentTurnRequest(message="work", invocation_id="turn")
    key = AgentSessionKey(SessionScope.MEMBER, "worker:member")
    return executor.run(session_spec=spec, turn=request, session_key=key).text


def _executors(chunks: list[str]) -> dict[str, AgentClient | FakeAgentClient]:
    """The production client over a scripted provider, and the Fake, both told the same reply."""
    fake = FakeAgentClient(
        capabilities=AgentCapabilities(provider_session_resume=True, session_reuse=True)
    )
    fake.set_text("worker", "".join(chunks))
    fake.stream_output("worker", chunks)
    return {
        "client": AgentClient(
            FakeProvider(answer="".join(chunks), turn=[assistant_text(c) for c in chunks])
        ),
        "fake": fake,
    }


@pytest.mark.parametrize("implementation", ["client", "fake"])
@given(
    head=st.text(alphabet='{"reasoning": ', max_size=20),
    padding=_PADDING,
    pieces=st.integers(min_value=1, max_value=4),
)
@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
def test_a_whitespace_flood_fails_the_turn_however_it_is_chunked(
    tmp_path_factory: pytest.TempPathFactory,
    implementation: str,
    head: str,
    padding: str,
    pieces: int,
) -> None:
    flood = padding * (MAX_WHITESPACE_RUN // len(padding) + 2)
    size = len(flood) // pieces + 1
    chunks = [head] + [flood[i : i + size] for i in range(0, len(flood), size)]
    executor = _executors(chunks)[implementation]
    try:
        with pytest.raises(AgentOutputSchemaError, match=ReplyBreach.DEGENERATE):
            _turn(executor, tmp_path_factory.mktemp("turn"))
    finally:
        executor.close()


@pytest.mark.parametrize("implementation", ["client", "fake"])
@given(filler=_FILLER)
@settings(max_examples=examples(pr=5, nightly=6), deadline=None)
def test_an_oversized_reply_fails_the_turn(
    tmp_path_factory: pytest.TempPathFactory, implementation: str, filler: str
) -> None:
    # Filler broken by single spaces is not whitespace-degenerate; it is only too long.
    chunk = (filler + " ") * (MAX_REPLY_BYTES // (len(filler) + 1) // 2 + 1)
    executor = _executors([chunk, chunk, chunk])[implementation]
    try:
        with pytest.raises(AgentOutputSchemaError, match=ReplyBreach.OVERSIZED):
            _turn(executor, tmp_path_factory.mktemp("turn"))
    finally:
        executor.close()


@pytest.mark.parametrize("implementation", ["client", "fake"])
def test_a_reply_within_the_limits_is_untouched(tmp_path: Path, implementation: str) -> None:
    text = '{"value": 7}' + " " * (MAX_WHITESPACE_RUN - 1) + "\n"
    executor = _executors([text])[implementation]
    try:
        assert _turn(executor, tmp_path) == text
    finally:
        executor.close()


@given(text=st.text(max_size=300))
def test_short_replies_are_never_refused(text: str) -> None:
    assert reply_breach(text) is None


@given(chunks=st.lists(st.text(max_size=40), max_size=12))
def test_the_verdict_does_not_depend_on_chunking(chunks: list[str]) -> None:
    guard = ReplyGuard()
    verdict = None
    for chunk in chunks:
        verdict = guard.feed(chunk) or verdict
    assert (verdict is None) == (reply_breach("".join(chunks)) is None)
