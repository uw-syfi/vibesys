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
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_agent.api import (
    CONTEXT_RENEWAL_FRACTION,
    AgentClient,
    AgentContextOverflowError,
    AgentExecutionPolicy,
    AgentQuotaError,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    ContextUse,
    DurableSessionStore,
    QuotaCondition,
    SessionScope,
    SessionStore,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider, FakeTurnScript
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_prompts.api import TemplateRenderer
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import CapacityStops

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


def _overflow() -> Exception:
    return AgentContextOverflowError("claude", "Prompt is too long")


def _world(
    workspace: Path,
    *,
    context_tokens: tuple[int | None, ...] = (),
    overflow_on: frozenset[int] = frozenset(),
    fail_on: Mapping[int, Callable[[], Exception]] | None = None,
    store: SessionStore | None = None,
) -> _World:
    """A world whose provider reports *context_tokens* per conversation turn.

    The provider refuses the sends numbered in *overflow_on* (1-based, across conversations)
    for context overflow, and fails the sends numbered in *fail_on* with the error built
    there. A capacity stop ends its turn, as a run's quota policy that gives up does.
    """
    sent: list[AgentTurnRequest] = []
    failures: dict[int, Callable[[], Exception]] = dict.fromkeys(overflow_on, _overflow)
    failures.update(fail_on or {})

    def on_turn(request: AgentTurnRequest) -> None:
        sent.append(request)
        failure = failures.get(len(sent))
        if failure is not None:
            raise failure()

    threads = SimThreads()
    provider = FakeProvider(
        script=FakeTurnScript(("done",), context_tokens=context_tokens),
        on_turn=on_turn,
        threads=threads,
    )
    client = AgentClient(provider, clock=ManualClock(), threads=threads, session_store=store)
    client.set_capacity_gate(_GiveUp())
    sessions = ClientAgentSessions(client, FakeAgentInvocationStore(), threads=threads)
    return _World(sessions, client, workspace, sent)


class _GiveUp:
    """A capacity gate that ends every stopped turn, as a run's quota policy does on a pause."""

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        del turn, role, stops
        raise error

    def turn_served(self, provider: str, model: str | None) -> None:
        del provider, model


def _durable_store(root: Path) -> DurableSessionStore:
    """The machine-local checkpoint store a run wires, in a fresh project at *root*."""
    root.mkdir()
    project = Project.open(root)
    project.state.create_project("renewal", now=datetime(2026, 1, 1, tzinfo=UTC))
    project.state.create_run(
        project.state.new_run_manifest(
            "renewal",
            run_id="renewal",
            branch="test",
            vibesys_version="test",
            trusted_input_baseline="a" * 40,
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=RunExecutionRecord(
                model="test",
                agent_backend="stub",
                compute_backend="cpu",
                requested_profiler="none",
                resolved_profiler="none",
                agent_roles={},
            ),
            orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    return DurableSessionStore(
        project.state.local_namespace("renewal", "agent").slot("sessions.json", AgentSessionState),
        threads=SimThreads(),
    )


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


_FRESH_SEND_FAILURES: dict[str, Callable[[], Exception]] = {
    "unavailable": lambda: AgentQuotaError(
        "claude", QuotaCondition.UNAVAILABLE, "API Error: 503 Service Unavailable"
    ),
    "rate-limited": lambda: AgentQuotaError(
        "claude", QuotaCondition.RATE_LIMITED, "API Error: 429 rate limited"
    ),
    "quota": lambda: AgentQuotaError(
        "claude", QuotaCondition.QUOTA_EXHAUSTED, "usage limit reached"
    ),
    "overflow": _overflow,
    "other": lambda: RuntimeError("claude exited with status 1"),
}


@pytest.mark.regression
@pytest.mark.parametrize("failure", sorted(_FRESH_SEND_FAILURES))
@pytest.mark.parametrize("trigger", ["pre-emptive", "overflow"])
def test_a_failed_fresh_conversation_leaves_the_old_one_resumable(
    tmp_path: Path, failure: str, trigger: str
) -> None:
    """A renewal whose fresh send fails keeps the retired conversation's checkpoint.

    The fresh conversation replaces the checkpoint only once it completed a turn. Until then
    the checkpoint on disk names the old conversation, so a crash during the fresh send, a
    failure of it, or a later resend (after a quota wait, say) can still resume the key.
    """
    store = _durable_store(tmp_path / "project")
    # Send 1 opens the conversation. A pre-emptive renewal sends the fresh turn as send 2;
    # an overflow-triggered one first resumes the old conversation (send 2, refused), then
    # sends the fresh turn as send 3.
    fresh_send = 2 if trigger == "pre-emptive" else 3
    on_disk: list[str | None] = []

    def fail_fresh_send() -> Exception:
        record = store.get(_KEY)
        on_disk.append(None if record is None else record.session_id)
        return _FRESH_SEND_FAILURES[failure]()

    world = _world(
        tmp_path,
        context_tokens=(850_000,) if trigger == "pre-emptive" else (),
        overflow_on=frozenset({2}) if trigger == "overflow" else frozenset(),
        fail_on={fresh_send: fail_fresh_send},
        store=store,
    )
    try:
        first = world.first()
        failed = world.resume("second", standalone=True)

        assert isinstance(failed, Unknown), failed
        assert len(world.sent) == fresh_send
        assert world.sent[-1].expected_provider_session_id is None
        # What a crash during the fresh send would have left on disk.
        assert on_disk == [first.checkpoint.provider_session_id]
        assert world.sessions.checkpoint(_KEY) == first.checkpoint

        # The runtime settles the Unknown turn (CancelTurn releases it), then sends again.
        world.sessions.release_interrupted(_KEY, "second")
        resent = world.resume("third", standalone=True)
        assert isinstance(resent, Completed), resent
        if trigger == "pre-emptive":
            # The retired conversation's context reading survived too, so it is retired again.
            assert world.sent[-1].expected_provider_session_id is None
            assert resent.checkpoint != first.checkpoint
    finally:
        world.client.close()


def test_a_completed_renewal_replaces_the_checkpoint_on_disk(tmp_path: Path) -> None:
    """The fresh conversation is opened without the old checkpoint, and then replaces it."""
    store = _durable_store(tmp_path / "project")
    world = _world(tmp_path, context_tokens=(850_000,), store=store)
    try:
        first = world.first()
        renewed = world.resume("second", standalone=True)

        assert isinstance(renewed, Completed), renewed
        assert renewed.checkpoint != first.checkpoint
        record = store.get(_KEY)
        assert record is not None
        assert record.session_id == renewed.checkpoint.provider_session_id
    finally:
        world.client.close()


def test_a_fresh_conversation_that_overflows_after_a_pre_emptive_renewal_is_not_retried(
    tmp_path: Path,
) -> None:
    """A message too long for an empty conversation is refused once, not renewed again.

    The retired conversation's checkpoint is still on disk while the fresh one runs, so the
    key still names a conversation to renew from: only the turn's own history may stop it.
    """
    world = _world(
        tmp_path,
        context_tokens=(850_000,),
        overflow_on=frozenset({2, 3}),
        store=_durable_store(tmp_path / "project"),
    )
    try:
        world.first()
        failed = world.resume("second", standalone=True)

        assert isinstance(failed, Unknown), failed
        assert "AgentContextOverflowError" in failed.detail
        assert len(world.sent) == 2
        assert world.sent[-1].expected_provider_session_id is None
    finally:
        world.client.close()
