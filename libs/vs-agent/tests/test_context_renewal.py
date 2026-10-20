"""A keyed conversation near or past its model's context window continues in a fresh one.

A resumed conversation carries every earlier tool result, so a long one can outgrow the
model's context window: the provider then refuses the turn ("Prompt is too long") and the
round is lost (S185). Two rules keep a turn whose message stands alone (it carries the
role's whole prompt, not a correction of the last reply) out of that:

- before resuming, a conversation whose last turn reported context use at or above
  ``CONTEXT_RENEWAL_FRACTION`` of the model's window is retired, and the turn runs in a
  fresh conversation with the same rendered instructions and message;
- a resume the provider refuses for context overflow runs once more in a fresh
  conversation, within the same invocation, and a second overflow is not retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_agent.api import (
    CONTEXT_RENEWAL_FRACTION,
    AgentClient,
    AgentContextOverflowError,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    ContextUse,
    SessionScope,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider, FakeTurnScript
from vs_prompts.api import TemplateRenderer
from vs_sim.api.testing import ManualClock, SimThreads, examples

_MODEL = "claude-opus-4-7"
_WINDOW = 1_000_000
"""The context window agentshim knows for ``_MODEL``."""
_INSTRUCTIONS = "Implement the hypothesis."
_KEY = AgentSessionKey(SessionScope.MEMBER, "implementer:worker")


@dataclass
class _World:
    """One client over a scripted provider, its durable sessions, and every turn it sent."""

    sessions: ClientAgentSessions
    client: AgentClient
    workspace: Path
    sent: list[AgentTurnRequest] = field(default_factory=list)

    @property
    def spec(self) -> AgentSessionSpec:
        return AgentSessionSpec(
            role="implementer",
            provider="claude",
            model=_MODEL,
            workspace=self.workspace,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )

    def first(self) -> Completed:
        """The conversation's first turn, then the binding its later turns resume with."""
        turn = AgentTurnRequest(message="work", instructions=_INSTRUCTIONS, invocation_id="first")
        outcome = self.sessions.start(_KEY, self.spec, turn)
        assert isinstance(outcome, Completed), outcome
        self.bind(outcome)
        return outcome

    def bind(self, outcome: Completed) -> None:
        """Bind the key as the runtime does: to the conversation it last completed in."""
        self.sessions.bind(
            _KEY,
            self.spec,
            AgentTurnRequest(
                message="",
                instructions=_INSTRUCTIONS,
                expected_provider_session_id=outcome.checkpoint.provider_session_id,
            ),
        )

    def resume(self, invocation: str, *, standalone: bool) -> object:
        message = TemplateRenderer(self.workspace).render_string(
            "attempt {{ n }}: notes and evidence", n=invocation
        )
        return self.sessions.resume(_KEY, message, invocation, standalone=standalone)


def _world(
    workspace: Path,
    *,
    context_tokens: tuple[int | None, ...] = (),
    overflow_on: frozenset[int] = frozenset(),
) -> _World:
    """A world whose provider reports *context_tokens* per conversation turn.

    The provider refuses the sends numbered in *overflow_on* (1-based, across conversations)
    for context overflow.
    """
    sent: list[AgentTurnRequest] = []

    def on_turn(request: AgentTurnRequest) -> None:
        sent.append(request)
        if len(sent) in overflow_on:
            message = "Prompt is too long"
            raise AgentContextOverflowError("claude", message)

    threads = SimThreads()
    provider = FakeProvider(
        script=FakeTurnScript(("done",), context_tokens=context_tokens),
        on_turn=on_turn,
        threads=threads,
    )
    client = AgentClient(provider, clock=ManualClock(), threads=threads)
    sessions = ClientAgentSessions(client, FakeAgentInvocationStore(), threads=threads)
    return _World(sessions, client, workspace, sent)


def test_a_conversation_at_85_percent_of_its_window_resumes_in_a_fresh_one(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, context_tokens=(850_000,))
    try:
        first = world.first()
        renewed = world.resume("second", standalone=True)

        assert isinstance(renewed, Completed), renewed
        assert renewed.checkpoint != first.checkpoint
        fresh = world.sent[-1]
        # The fresh conversation is seeded with the turn's own rendered prompt, nothing else.
        assert fresh.expected_provider_session_id is None
        assert fresh.instructions == _INSTRUCTIONS
        assert fresh.message == "attempt second: notes and evidence"

        # A turn that answers the conversation (a correction) continues the fresh one.
        world.bind(renewed)
        corrected = world.resume("second/correction", standalone=False)
        assert isinstance(corrected, Completed), corrected
        assert corrected.checkpoint == renewed.checkpoint
    finally:
        world.client.close()


def test_a_resume_refused_for_context_overflow_runs_once_more_in_a_fresh_conversation(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, overflow_on=frozenset({2}))
    try:
        first = world.first()
        renewed = world.resume("second", standalone=True)

        assert isinstance(renewed, Completed), renewed
        assert renewed.checkpoint != first.checkpoint
        assert len(world.sent) == 3
        assert world.sent[1].expected_provider_session_id == first.checkpoint.provider_session_id
        assert world.sent[2].expected_provider_session_id is None
        assert world.sent[2].message == world.sent[1].message
    finally:
        world.client.close()


def test_a_second_context_overflow_is_not_retried(tmp_path: Path) -> None:
    world = _world(tmp_path, overflow_on=frozenset({2, 3, 4}))
    try:
        world.first()
        failed = world.resume("second", standalone=True)

        assert isinstance(failed, Unknown), failed
        assert "AgentContextOverflowError" in failed.detail
        assert len(world.sent) == 3
    finally:
        world.client.close()


def test_an_overflow_of_a_turn_that_answers_the_conversation_is_not_retried_fresh(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, overflow_on=frozenset({2}))
    try:
        world.first()
        failed = world.resume("second/correction", standalone=False)

        assert isinstance(failed, Unknown), failed
        assert "AgentContextOverflowError" in failed.detail
        assert len(world.sent) == 2
    finally:
        world.client.close()


@settings(max_examples=examples(pr=10, nightly=100))
@given(tokens=st.integers(0, _WINDOW), standalone=st.booleans())
@example(tokens=799_999, standalone=True)
@example(tokens=800_000, standalone=True)
@example(tokens=_WINDOW, standalone=False)
def test_a_resume_starts_fresh_exactly_when_it_stands_alone_and_use_reached_the_threshold(
    tokens: int, *, standalone: bool
) -> None:
    with TemporaryDirectory() as raw:
        world = _world(Path(raw), context_tokens=(tokens,))
        try:
            first = world.first()
            resumed = world.resume("second", standalone=standalone)
        finally:
            world.client.close()

    assert isinstance(resumed, Completed), resumed
    near = Fraction(tokens, _WINDOW) >= Fraction(CONTEXT_RENEWAL_FRACTION)
    assert (resumed.checkpoint != first.checkpoint) == (standalone and near)


@settings(max_examples=examples(pr=50, nightly=500))
@given(
    tokens=st.integers(0, 4_000_000),
    window=st.one_of(st.none(), st.integers(1, 4_000_000)),
    threshold=st.fractions(min_value=Fraction(1, 100), max_value=1),
)
def test_context_use_is_near_its_limit_exactly_at_and_above_the_threshold(
    tokens: int, window: int | None, threshold: Fraction
) -> None:
    use = ContextUse(tokens=tokens, window=window)
    near = use.near_limit(threshold)

    assert near == (window is not None and tokens >= threshold * window)
    if near:
        assert ContextUse(tokens=tokens + 1, window=window).near_limit(threshold)
