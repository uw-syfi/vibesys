"""Splitting an ``event_batch`` into chunks preserves what folding it means."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

from hypothesis import example, given, settings
from hypothesis import strategies as st

from server.api.protocol import EventBatchMessage
from server.events import EventType, RunEvent
from vs_sim.api.testing import examples

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# Multi-byte and astral characters, because the budget is in UTF-8 bytes and
# ``model_dump_json`` emits them raw: an accounting in characters would pass on
# ASCII alone.
_TEXTS = st.lists(st.text(alphabet="ab é水\U0001d11e", max_size=300), max_size=30)


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
    - Each chunk fits the budget unless it is a lone event (or the empty
      checkpoint) that alone does not.
    - No split is premature: two neighbors joined under the batch's longest
      envelope would not have fit.
    """
    batch = _batch(texts, watermark_gap=watermark_gap, rebootstrap=rebootstrap, floor=floor)

    chunks = list(batch.split_within(budget))

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
        assert len(chunk.model_dump_json().encode()) <= budget or len(chunk.events) <= 1
    for left, right in pairwise(chunks):
        merged = batch.model_copy(
            update={"events": [*left.events, *right.events], "rebootstrap": False}
        )
        assert len(merged.model_dump_json().encode()) > budget
