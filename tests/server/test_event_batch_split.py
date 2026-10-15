"""Splitting an ``event_batch`` into chunks preserves what folding it means."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from server.api.protocol import (
    MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES,
    MAX_SERVER_MESSAGE_BYTES,
    ActiveExecutions,
    ActiveExecutionsChunkMessage,
    EventBatchMessage,
)
from server.events import AgentExecutionActivityData, EventType, RunEvent
from server.execution import ActiveAgentExecution
from vs_sim.api.testing import examples

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# Multi-byte and astral characters, because the budget is in UTF-8 bytes and
# ``model_dump_json`` emits them raw: an accounting in characters would pass on
# ASCII alone.
_TEXTS = st.lists(st.text(alphabet="ab é水\U0001d11e", max_size=300), max_size=30)


class _CountingTimezone(tzinfo):
    """Valid deterministic timezone that exposes JSON datetime visits."""

    def __init__(self) -> None:
        self.utcoffset_calls = 0

    def utcoffset(self, _value: datetime | None) -> timedelta:
        self.utcoffset_calls += 1
        return timedelta(0)

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "UTC"


def _batch(
    texts: list[str], *, watermark_gap: int, rebootstrap: bool, floor: int
) -> EventBatchMessage:
    events = [
        RunEvent(
            sequence=floor + index + 1,
            run_id="run",
            timestamp=_EPOCH,
            type=EventType.OUTPUT,
            text=text,
        )
        for index, text in enumerate(texts)
    ]
    return EventBatchMessage(
        events=events,
        through_sequence=floor + len(events) + watermark_gap,
        history_after_sequence=floor,
        store_id="store",
        rebootstrap=rebootstrap,
    )


def _active(index: int, assignment: str) -> ActiveAgentExecution:
    return ActiveAgentExecution(
        execution_id=f"execution-{index}",
        agent_kind="implementer",
        round_label="round-1",
        stage="implementer",
        assignment=assignment,
        started_at=_EPOCH,
        activity=AgentExecutionActivityData(mode="thinking", summary="working"),
    )


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    texts=_TEXTS,
    budget=st.integers(min_value=0, max_value=6000),
    watermark_gap=st.integers(min_value=0, max_value=10**6),
    rebootstrap=st.booleans(),
    floor=st.integers(min_value=0, max_value=10**9),
)
@example(texts=["x"] * 5, budget=0, watermark_gap=0, rebootstrap=True, floor=0)
# A later chunk spells ``"rebootstrap":false``, one byte longer than the first
# chunk's ``true``; at this budget the second chunk lands exactly on the limit.
@example(texts=[""] * 4, budget=654, watermark_gap=0, rebootstrap=True, floor=0)
def test_folding_the_chunks_in_order_is_folding_the_batch(
    *, texts: list[str], budget: int, watermark_gap: int, rebootstrap: bool, floor: int
) -> None:
    """Every chunk is a whole batch, and the chunks together mean the batch.

    - The events arrive once each, in order; no chunk is empty unless the
      batch is, since an empty checkpoint still reports its watermark.
    - Only the first chunk can supersede the fold (``WP-REBOOTSTRAP``). A
      later chunk carrying ``rebootstrap`` would tell a consumer to discard the
      earlier chunks of the same replay.
    - Each cursor is the last sequence its chunk carried, except the final
      one, which keeps the batch's watermark.
    - Every returned chunk fits the budget; an impossible event or checkpoint
      rejects the projection instead of being mislabeled as bounded.
    - No split is premature: two neighbors joined under the batch's longest
      envelope would not have fit.
    """
    batch = _batch(texts, watermark_gap=watermark_gap, rebootstrap=rebootstrap, floor=floor)

    try:
        chunks = list(batch.split_within(budget))
    except ValueError:
        return

    assert chunks
    assert [event for chunk in chunks for event in chunk.events] == batch.events
    assert all(chunk.events for chunk in chunks) or chunks == [batch]
    assert [chunk.rebootstrap for chunk in chunks] == [rebootstrap] + [False] * (len(chunks) - 1)
    assert [chunk.through_sequence for chunk in chunks] == [
        *(chunk.events[-1].sequence for chunk in chunks[:-1]),
        batch.through_sequence,
    ]
    assert all(
        chunk.model_copy(update={"events": [], "through_sequence": 0, "rebootstrap": False})
        == batch.model_copy(update={"events": [], "through_sequence": 0, "rebootstrap": False})
        for chunk in chunks
    )
    for chunk in chunks:
        assert len(chunk.model_dump_json().encode()) <= budget
    for left, right in pairwise(chunks):
        merged = batch.model_copy(
            update={"events": [*left.events, *right.events], "rebootstrap": False}
        )
        assert len(merged.model_dump_json().encode()) > budget


def test_whole_batch_split_rejects_an_oversized_empty_checkpoint() -> None:
    """The legacy whole-batch API never calls an oversized checkpoint bounded."""
    batch = EventBatchMessage(
        events=[],
        active_executions=[_active(0, "x" * 1_100_000)],
    )

    with pytest.raises(ValueError, match="frame budget"):
        next(batch.split_within(MAX_SERVER_MESSAGE_BYTES))


@settings(max_examples=examples(pr=30, nightly=300))
@given(
    assignments=st.lists(
        st.text(alphabet='ab\0\n\t "\\é水\U0001d11e', min_size=1, max_size=1_000),
        min_size=1,
        max_size=8,
    ),
    budget=st.integers(min_value=0, max_value=5_000),
)
def test_stream_messages_bound_and_reconstruct_every_execution_checkpoint(
    assignments: list[str], budget: int
) -> None:
    """Unicode/escaping and aggregate size never escape the frame budget."""
    active = [_active(index, assignment) for index, assignment in enumerate(assignments)]
    batch = EventBatchMessage(events=[], through_sequence=7, active_executions=active)

    error_message: str | None = None
    try:
        messages = list(batch.stream_messages_within(budget))
    except ValueError as error:
        error_message = str(error)
        messages = []
    if error_message is not None:
        assert "frame budget" in error_message or "fragment-count limit" in error_message
        return

    assert messages
    assert all(len(message.model_dump_json().encode()) <= budget for message in messages)
    fragments = [
        message for message in messages if isinstance(message, ActiveExecutionsChunkMessage)
    ]
    if fragments:
        event_batches = [message for message in messages if isinstance(message, EventBatchMessage)]
        assert [message.active_executions for message in event_batches] == [None]
        assert {message.active_executions_checkpoint_id for message in event_batches} == {
            fragments[0].checkpoint_id
        }
        assert [message.chunk_index for message in fragments] == list(range(len(fragments)))
        assert {message.chunk_count for message in fragments} == {len(fragments)}
        assert (
            fragments[0].checkpoint_id
            == hashlib.sha256(TypeAdapter(ActiveExecutions).dump_json(active)).hexdigest()
        )
        reconstructed = TypeAdapter(ActiveExecutions).validate_json(
            "".join(message.payload for message in fragments)
        )
        assert reconstructed == active
    else:
        assert messages == [batch]


@settings(max_examples=examples(pr=30, nightly=300))
@given(
    texts=st.lists(
        st.text(alphabet='ab\0\n "\\é水\U0001d11e', max_size=200),
        max_size=5,
    ),
    assignment=st.text(alphabet='xy\0 "\\é水\U0001d11e', max_size=500),
    budget=st.integers(min_value=0, max_value=1_200),
    rebootstrap=st.booleans(),
)
def test_stream_projection_never_emits_an_oversized_event_or_checkpoint_frame(
    *, texts: list[str], assignment: str, budget: int, rebootstrap: bool
) -> None:
    """Every accepted small-budget projection preserves all whole events."""
    event_batch = _batch(texts, watermark_gap=3, rebootstrap=rebootstrap, floor=4)
    batch = event_batch.model_copy(update={"active_executions": [_active(0, assignment)]})

    error_message: str | None = None
    try:
        messages = list(batch.stream_messages_within(budget))
    except ValueError as error:
        error_message = str(error)
        messages = []
    if error_message is not None:
        assert "frame budget" in error_message or "fragment-count limit" in error_message
        return

    assert messages
    assert all(len(message.model_dump_json().encode()) <= budget for message in messages)
    projected_events = [
        event
        for message in messages
        if isinstance(message, EventBatchMessage)
        for event in message.events
    ]
    assert projected_events == batch.events


@pytest.mark.parametrize(
    "assignments",
    [
        pytest.param(["x" * 1_100_000], id="one-large-assignment"),
        pytest.param(["x" * 45_000 for _ in range(25)], id="aggregate-concurrent-assignments"),
    ],
)
def test_oversized_execution_checkpoint_regressions_fit_the_stock_peer(
    assignments: list[str],
) -> None:
    """Both the single-prompt and realistic aggregate base failures are bounded."""
    batch = EventBatchMessage(
        events=[],
        active_executions=[
            _active(index, assignment) for index, assignment in enumerate(assignments)
        ],
    )

    messages = list(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES))

    assert len(messages) > 1
    assert all(
        len(message.model_dump_json().encode()) <= MAX_SERVER_MESSAGE_BYTES for message in messages
    )


def test_near_limit_checkpoint_fragments_when_an_event_makes_the_envelope_oversize() -> None:
    """The checkpoint and its event must fit together, not only in isolation."""
    batch = EventBatchMessage(
        events=_batch(["combined envelope"], watermark_gap=0, rebootstrap=False, floor=0).events,
        through_sequence=1,
        active_executions=[_active(0, "x" * 1_047_874)],
        store_id="store",
    )

    assert (
        len(batch.model_copy(update={"events": []}).model_dump_json().encode())
        <= MAX_SERVER_MESSAGE_BYTES
    )
    assert len(batch.model_dump_json().encode()) > MAX_SERVER_MESSAGE_BYTES
    messages = list(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES))

    assert any(isinstance(message, ActiveExecutionsChunkMessage) for message in messages)
    assert all(
        len(message.model_dump_json().encode()) <= MAX_SERVER_MESSAGE_BYTES for message in messages
    )
    batches = [message for message in messages if isinstance(message, EventBatchMessage)]
    assert [event for message in batches for event in message.events] == batch.events
    assert all(message.active_executions is None for message in batches)


def test_checkpoint_above_the_aggregate_limit_fails_before_any_event_frame() -> None:
    """A rejected checkpoint is not encoded and cannot advance the replay cursor."""
    batch = EventBatchMessage(
        events=_batch(["already recorded"], watermark_gap=0, rebootstrap=False, floor=0).events,
        through_sequence=1,
        # Pydantic's JSON encoder rejects the final lone surrogate. Receiving
        # the aggregate-limit error proves the O(1) character lower bound
        # rejected this input before invoking that encoder or allocating its
        # JSON representation.
        active_executions=[_active(0, "x" * MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES + "\ud800")],
        store_id="store",
    )

    messages = batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES)

    with pytest.raises(ValueError, match="16777216-byte protocol limit"):
        next(messages)


def test_escaped_checkpoint_is_rejected_before_authoritative_encoding() -> None:
    """JSON escaping cannot turn a short string into an unbounded allocation."""
    batch = EventBatchMessage(
        events=[],
        # Each NUL is six JSON bytes. The trailing surrogate would make the
        # authoritative encoder fail if the bounded preflight did not reject
        # once the escape-aware byte count crosses the aggregate cap.
        active_executions=[
            _active(0, "\0" * (MAX_ACTIVE_EXECUTIONS_CHECKPOINT_BYTES // 6 + 1) + "\ud800")
        ],
    )

    with pytest.raises(ValueError, match="16777216-byte protocol limit"):
        next(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES))


def test_too_small_budget_rejects_before_yielding_an_oversized_empty_batch() -> None:
    """A deferred cursor frame is never yielded above the caller's cap."""
    batch = EventBatchMessage(
        events=[],
        active_executions=[_active(0, "x" * 1_000)],
    )
    messages = batch.stream_messages_within(234)

    with pytest.raises(ValueError, match="frame budget 234 cannot hold an event_batch frame"):
        next(messages)


def test_checkpoint_size_preflight_matches_the_canonical_json_boundary() -> None:
    """All variable checkpoint strings use the canonical JSON byte accounting."""
    active = _active(0, 'assignment\0\n"\\é水\U0001d11e').model_copy(
        update={
            "execution_id": 'execution\0"',
            "agent_kind": "kind水",
            "round_label": "round\n",
            "stage": "stage\\",
            "activity": AgentExecutionActivityData(
                mode="tool",
                summary='summary\0"',
                tool="toolé",
            ),
            "provider": "provider水",
            "model": "model\t",
        }
    )
    batch = EventBatchMessage(events=[], active_executions=[active])
    exact_size = len(batch.model_dump_json().encode())

    assert list(batch.stream_messages_within(exact_size)) == [batch]
    smaller_messages = list(batch.stream_messages_within(exact_size - 1))
    assert all(
        len(message.model_dump_json().encode()) <= exact_size - 1 for message in smaller_messages
    )
    assert any(isinstance(message, ActiveExecutionsChunkMessage) for message in smaller_messages)


@settings(max_examples=examples(pr=30, nightly=300))
@given(value=st.text(max_size=200))
def test_checkpoint_preflight_matches_canonical_utf8_for_all_unicode(value: str) -> None:
    """Valid Unicode never disagrees with Pydantic's authoritative encoding."""
    batch = EventBatchMessage(events=[], active_executions=[_active(0, value)])

    assert list(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES)) == [batch]


def test_checkpoint_preflight_preserves_lone_surrogate_encoding_failure() -> None:
    """Sizing does not replace invalid Unicode or give it a nonstandard digest."""
    batch = EventBatchMessage(events=[], active_executions=[_active(0, "\ud800")])

    with pytest.raises(PydanticSerializationError, match="surrogates not allowed"):
        next(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES))


@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param("inline", id="ordinary-frame"),
        pytest.param("x" * 1_100_000, id="fragmented-checkpoint"),
    ],
)
def test_projection_serializes_the_authoritative_checkpoint_once(assignment: str) -> None:
    """Sizing uses one bounded skeleton pass and one authoritative encoding."""
    timezone = _CountingTimezone()
    active = _active(0, assignment).model_copy(
        update={"started_at": datetime(2026, 1, 1, tzinfo=timezone)}
    )
    batch = EventBatchMessage(events=[], active_executions=[active])

    list(batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES))

    # test-isolation: datetime visits are the public structural signal that
    # the expensive checkpoint encoder is not called repeatedly.
    assert timezone.utcoffset_calls == 2
