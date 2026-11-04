"""Mirroring a journal into the run log costs in proportion to its length, live and on relaunch.

The tee projects every journal event as the journal writes it, under the journal's lock, and a
relaunch replays the whole journal through the same projection before the run environment opens.
Projecting an event used to decode and encode every span of the run so far, so a 600 idea run's
appends grew from 6 ms to 16 ms each and a relaunch held the run for 30 s of CPU. The cost is
counted, not timed: the protobuf decodes and encodes a replay of ``n``, ``4 * n`` and ``16 * n``
events makes, whole path included (the projection, the writer and the fold of what it wrote).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from tests.support.codec_work import CodecWork, codec_work

import vibesys.events as e
from vibesys.run.runlog_tee import RunLogTee
from vs_runlog.api import FileRunContent, FileRunLogWriter
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_RUN = "run-a"

# A fourfold step in events adds four times the cost of the step before it when the work is
# linear in the journal, and sixteen times when it is quadratic. Comparing the added costs, not
# the totals, leaves out the fixed cost of opening a run, which hides a quadratic part.
_LINEAR = 6
_SIZES = (12, 48, 192)


def _journal(length: int) -> list[e.CoreEvent]:
    """``length`` events of a run in progress: work started and finished, output, and noise."""
    events: list[e.CoreEvent] = []
    for index in range(length):
        step, role = divmod(index, 4)
        identity = f"op:work-{step}"
        wire = {
            "id": identity,
            "parent": None,
            "type": "operation",
            "name": "work",
            "status": "running" if role == 0 else "done",
            "started_at": step,
            "ended_at": None if role == 0 else step + 1,
            "invocation": f"turn-{step}",
            "label": {"kind": "agent_turn", "attrs_json": json.dumps({"agent": "a", "role": "r"})},
        }
        if role == 0:
            kind, data = (
                e.CoreEventType.SPAN_STARTED,
                e.SpanStartedData.model_validate({"span": wire}),
            )
        elif role == 1:
            kind = e.CoreEventType.AGENT_OUTPUT_CHUNK
            data = e.AgentOutputChunkData(channel="assistant", content=f"chunk {step}")
        elif role == 2:
            kind, data = e.CoreEventType.TODO_UPDATE, e.TodoUpdateData()
        else:
            kind, data = e.CoreEventType.SPAN_ENDED, e.SpanEndedData.model_validate({"span": wire})
        events.append(
            e.CoreEvent.model_validate(
                {
                    "type": kind,
                    "data": data,
                    "timestamp": _AT,
                    "run_id": _RUN,
                    "sequence": index + 1,
                    "execution_id": f"turn-{step}",
                }
            )
        )
    return events


def _tee(root: Path, history: list[e.CoreEvent]) -> RunLogTee:
    return RunLogTee(
        FileRunLogWriter(root / "run.log", clock=ManualClock(), threads=SimThreads()),
        FileRunContent(root / "blobs", root / "streams"),
        _RUN,
        history=lambda: history,
        checkout=("tree", False),
    )


def _relaunch(length: int) -> CodecWork:
    """The codec work of a relaunch that replays a journal of ``length`` events."""
    with TemporaryDirectory() as directory:
        tee = _tee(Path(directory), _journal(length))
        try:
            _, work = codec_work(lambda: tee.publish_successor(_AT))
        finally:
            tee.close()
    return work


def _live(length: int) -> CodecWork:
    """The codec work of mirroring a journal of ``length`` events as they are written."""
    payloads = [event.model_dump_json().encode() for event in _journal(length)]
    with TemporaryDirectory() as directory:
        tee = _tee(Path(directory), [])

        def mirror() -> None:
            for payload in payloads:
                tee.append(payload)

        try:
            _, work = codec_work(mirror)
        finally:
            tee.close()
    return work


def _grows_linearly(cost: Callable[[int], CodecWork]) -> tuple[CodecWork, ...] | None:
    """None when ``cost`` of ``n``, ``4 * n`` and ``16 * n`` events grows linearly, else the costs."""
    short, long, longest = (cost(length) for length in _SIZES)
    calls = longest.calls - long.calls <= _LINEAR * (long.calls - short.calls)
    size = longest.size - long.size <= _LINEAR * (long.size - short.size)
    return None if calls and size else (short, long, longest)


@pytest.mark.regression
def test_a_relaunch_replays_a_journal_in_time_proportional_to_its_length() -> None:
    """Replaying ``n``, ``4 * n`` and ``16 * n`` events: the added cost grows 4 times, not 16."""
    assert _grows_linearly(_relaunch) is None


@pytest.mark.regression
def test_each_mirrored_event_costs_the_same_however_long_the_journal_already_is() -> None:
    """Mirroring ``n``, ``4 * n`` and ``16 * n`` events one by one: the added cost grows 4 times."""
    assert _grows_linearly(_live) is None
