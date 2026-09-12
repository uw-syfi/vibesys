"""Tail subscription, bounded backfill, late-attach, and lifetime transport contracts."""

import json
import socket
import threading
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
from tests.server.support import ServerParts, build_server_parts

from server.api.protocol import EventsQuery, SnapshotQuery, SubscribeRequest
from server.api.service import RunApi
from server.events import (
    ChatData,
    ChatThreadCreatedData,
    EventData,
    EventStore,
    EventType,
    OutputData,
    RoundFinishedData,
    RunEvent,
    RunStartedData,
)
from server.journal import _BOOTSTRAP_SPINE_TYPES
from server.transport.unix_jsonl import UnixJsonlServer

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_ROUND_EVERY = 25


class _EventFields(TypedDict, total=False):
    """Run-event fields varied by the fixture builder."""

    data: EventData
    chat_thread_id: str
    round_label: str
    text: str


def _event(sequence: int, event_type: EventType, **fields: Unpack[_EventFields]) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="persisted-run",
        timestamp=_TIMESTAMP,
        type=event_type,
        **fields,
    )


def _round_log(count: int, *, with_threads: bool = False) -> list[RunEvent]:
    events = [
        _event(
            1,
            EventType.RUN_STARTED,
            data=RunStartedData(outer_loop="agent", input="objective", max_rounds=24),
        )
    ]
    if with_threads:
        events.append(
            _event(
                2,
                EventType.CHAT_THREAD_CREATED,
                chat_thread_id="thread-1",
                data=ChatThreadCreatedData(
                    thread_id="thread-1",
                    driver="agentshim",
                    provider="claude",
                    model="opus",
                    created_at=_TIMESTAMP,
                ),
            )
        )
    for sequence in range(len(events) + 1, count + 1):
        if sequence % _ROUND_EVERY == 0:
            events.append(
                _event(
                    sequence,
                    EventType.ROUND_FINISHED,
                    round_label=f"round-{sequence // _ROUND_EVERY}",
                    data=RoundFinishedData(attempts=1, judge_verdict="pass"),
                )
            )
        elif with_threads and sequence == count - 1:
            events.append(
                _event(
                    sequence,
                    EventType.CHAT,
                    text="why did round two regress?",
                    chat_thread_id="thread-1",
                    data=ChatData(answer="because", thread_title="why did round two regress?"),
                )
            )
        else:
            events.append(
                _event(
                    sequence,
                    EventType.OUTPUT,
                    text=f"line-{sequence}",
                    data=OutputData(stream="stdout", content=f"line-{sequence}"),
                )
            )
    return events


def _spine_records(floor: int) -> int:
    return 1 + floor // _ROUND_EVERY


def _write_log(log_dir: Path, events: list[RunEvent]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "run-events.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in events)
    )
    return log_dir


def _attach(tmp_path: Path, events: list[RunEvent]) -> ServerParts:
    return build_server_parts(_write_log(tmp_path / "logs", events))


@contextmanager
def _subscribed_client(
    socket_path: Path, request: SubscribeRequest
) -> Generator[Callable[[], dict]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(request.model_dump_json().encode() + b"\n")
            stream.flush()
            yield lambda: json.loads(stream.readline())


@contextmanager
def _live_subscription(api: RunApi, request: SubscribeRequest) -> Generator[Callable[[], dict]]:
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108
    with UnixJsonlServer(socket_path, api), _subscribed_client(socket_path, request) as read:
        yield read


def _subscribe(api: RunApi, request: SubscribeRequest) -> tuple[dict, dict]:
    with _live_subscription(api, request) as read:
        return read(), read()


@pytest.mark.parametrize("tail", [None, 40])
def test_subscription_replays_requested_history(tmp_path, tail):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    latest = parts.api.snapshot().sequence

    subscribed, batch = _subscribe(parts.api, SubscribeRequest(after_sequence=0, tail=tail))

    assert subscribed["type"] == "subscribed"
    floor = 0 if tail is None else latest - tail
    assert batch["history_after_sequence"] == floor
    assert batch["through_sequence"] == latest
    replayed_tail = [event["sequence"] for event in batch["events"] if event["sequence"] > floor]
    assert replayed_tail == list(range(floor + 1, latest + 1))


def test_tail_replays_pre_floor_run_spine_in_order(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    latest = parts.api.snapshot().sequence
    floor = latest - 40

    _subscribed, batch = _subscribe(parts.api, SubscribeRequest(after_sequence=0, tail=40))

    sequences = [event["sequence"] for event in batch["events"]]
    assert sequences == sorted(sequences)
    pre_floor = [event for event in batch["events"] if event["sequence"] <= floor]
    assert [event["type"] for event in pre_floor] == ["run_started"] + [
        "round_finished" for _ in range(floor // _ROUND_EVERY)
    ]


def test_tail_without_spine_events_delivers_only_suffix(tmp_path):  # noqa: ANN001, ANN201
    events = [
        _event(
            sequence,
            EventType.OUTPUT,
            data=OutputData(stream="stdout", content=f"line-{sequence}"),
        )
        for sequence in range(1, 121)
    ]
    parts = _attach(tmp_path, events)
    latest = parts.api.snapshot().sequence

    _subscribed, batch = _subscribe(parts.api, SubscribeRequest(after_sequence=0, tail=20))

    assert [event["sequence"] for event in batch["events"]] == list(range(latest - 19, latest + 1))


def _spine_type_values() -> set[str]:
    return {event_type.value for event_type in _BOOTSTRAP_SPINE_TYPES}


def test_bootstrap_tail_stays_bounded_when_events_land_mid_bootstrap(  # noqa: ANN201
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tail = 40
    parts = _attach(tmp_path, _round_log(200))
    original_bootstrap = parts.api.subscription_bootstrap
    pre_burst_watermarks: list[int] = []

    def bursty_bootstrap(after_sequence: int, tail_bound: int | None):  # noqa: ANN202
        # Reproduce the bootstrap race deterministically: a burst of appends
        # lands after the handler commits to bootstrapping but before the
        # bootstrap's own locked read.
        pre_burst_watermarks.append(parts.api.latest_sequence)
        for index in range(3000):
            parts.journal.publish_output("stdout", f"burst-{index}")
        return original_bootstrap(after_sequence, tail_bound)

    monkeypatch.setattr(parts.api, "subscription_bootstrap", bursty_bootstrap)

    subscribed, batch = _subscribe(parts.api, SubscribeRequest(after_sequence=0, tail=tail))

    floor = batch["history_after_sequence"]
    through = batch["through_sequence"]
    ordinary = [event for event in batch["events"] if event["sequence"] > floor]
    context = (
        f"pre-burst watermarks {pre_burst_watermarks or None}, checkpoint watermark {through}, "
        f"requested tail {tail}, ordinary bootstrap events {len(ordinary)}"
    )
    assert len(pre_burst_watermarks) == 1, context
    assert through == pre_burst_watermarks[0] + 3000, context
    assert len(ordinary) <= tail, context
    assert through == parts.api.latest_sequence, context
    assert floor == through - tail, context
    assert subscribed["latest_sequence"] == through, context
    pre_floor = [event for event in batch["events"] if event["sequence"] <= floor]
    assert all(event["type"] in _spine_type_values() for event in pre_floor), context
    assert all(event["sequence"] <= through for event in batch["events"]), context


def test_bootstrap_failure_surfaces_after_the_handshake(  # noqa: ANN201
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    parts = _attach(tmp_path, _round_log(50))
    latest = parts.api.latest_sequence

    def failing_bootstrap(_after_sequence: int, _tail: int | None):  # noqa: ANN202
        raise RuntimeError

    monkeypatch.setattr(parts.api, "subscription_bootstrap", failing_bootstrap)

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        subscribed = read()
        failure = read()

    # The dial itself must succeed: the client probes tail support by dialing
    # and treats a pre-handshake failure as a server without the field, so a
    # transient replay fault would otherwise trigger a full-history retry.
    assert subscribed["type"] == "subscribed"
    assert subscribed["latest_sequence"] == latest
    assert failure["type"] == "protocol_error"
    assert failure["code"] == "stream_failed"


def test_subscription_bootstrap_captures_one_atomic_state(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    latest = parts.api.latest_sequence

    bootstrap = parts.api.subscription_bootstrap(0, 40)

    assert bootstrap.run_id == parts.api.snapshot().run_id
    assert bootstrap.through_sequence == latest
    assert bootstrap.floor == latest - 40
    ordinary = [event.sequence for event in bootstrap.events if event.sequence > bootstrap.floor]
    assert ordinary == list(range(bootstrap.floor + 1, bootstrap.through_sequence + 1))
    pre_floor = [event for event in bootstrap.events if event.sequence <= bootstrap.floor]
    assert [event.type for event in pre_floor] == [EventType.RUN_STARTED] + [
        EventType.ROUND_FINISHED
    ] * (bootstrap.floor // _ROUND_EVERY)
    assert bootstrap.active_executions == []

    full = parts.api.subscription_bootstrap(0, None)
    assert full.floor == 0
    assert full.through_sequence == latest
    assert [event.sequence for event in full.events] == list(range(1, latest + 1))


def test_bootstrap_tail_stays_bounded_under_concurrent_appends(tmp_path):  # noqa: ANN001, ANN201
    tail = 10
    parts = _attach(tmp_path, _round_log(200))
    stop = threading.Event()

    def append_live_output() -> None:
        index = 0
        while not stop.is_set() and index < 20_000:
            parts.journal.publish_output("stdout", f"live-{index}")
            index += 1

    writer = threading.Thread(target=append_live_output)
    writer.start()
    try:
        _subscribed, batch = _subscribe(parts.api, SubscribeRequest(after_sequence=0, tail=tail))
    finally:
        stop.set()
        writer.join(timeout=5)
    assert not writer.is_alive()

    floor = batch["history_after_sequence"]
    ordinary = [event for event in batch["events"] if event["sequence"] > floor]
    assert len(ordinary) <= tail
    pre_floor = [event for event in batch["events"] if event["sequence"] <= floor]
    assert all(event["type"] in _spine_type_values() for event in pre_floor)
    assert all(event["sequence"] <= batch["through_sequence"] for event in batch["events"])


def test_checkpoint_parses_only_tail_and_spine(tmp_path):  # noqa: ANN001, ANN201
    count = 12_000
    parts = _attach(tmp_path, _round_log(count))
    store = parts.journal._store  # noqa: SLF001
    assert store is not None
    parsed_at_attach = store.parsed_record_count

    checkpoint = parts.api.subscription_checkpoint(count - 500, bootstrap_spine=True)

    assert store.parsed_record_count <= (parsed_at_attach + 500 + _spine_records(count - 500))
    assert store.parsed_record_count < count
    assert checkpoint.through_sequence >= count
    assert len(checkpoint.events) < count


def test_events_query_is_half_open_and_backfills_without_gaps(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    response = parts.api.execute(EventsQuery(after_sequence=50, before_sequence=60))
    assert [event.sequence for event in response.events] == list(range(51, 60))

    floor = 150
    collected: list[int] = []
    while floor > 0:
        after = max(0, floor - 40)
        response = parts.api.execute(EventsQuery(after_sequence=after, before_sequence=floor + 1))
        collected = [event.sequence for event in response.events] + collected
        floor = after
    assert collected == list(range(1, 151))


@pytest.mark.parametrize("before_sequence", [0, -1])
def test_events_query_rejects_meaningless_upper_bound(before_sequence):  # noqa: ANN001, ANN201
    with pytest.raises(ValueError):  # noqa: PT011
        EventsQuery(after_sequence=0, before_sequence=before_sequence)


def test_snapshot_reconstructs_chat_threads_from_full_history(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200, with_threads=True))

    response = parts.api.execute(SnapshotQuery())

    assert response.snapshot is not None
    assert [(thread.thread_id, thread.title) for thread in response.snapshot.chat_threads] == [
        ("thread-1", "why did round two regress?")
    ]
    assert response.snapshot.chat_threads[0].provider == "claude"


def test_late_attach_rebootstraps_at_fresh_tail_with_spine(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "server")

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        assert read()["type"] == "subscribed"
        bootstrap = read()
        parts.attach(_write_log(tmp_path / "logs", _round_log(200)))
        batch = read()

    assert bootstrap["history_after_sequence"] == 0
    latest = parts.api.snapshot().sequence
    floor = latest - 40
    assert batch["history_after_sequence"] == floor
    assert batch["through_sequence"] == latest
    pre_floor = [event for event in batch["events"] if event["sequence"] <= floor]
    assert [event["type"] for event in pre_floor] == ["run_started"] + [
        "round_finished" for _ in range(floor // _ROUND_EVERY)
    ]
    assert len(batch["events"]) <= 40 + _spine_records(floor)


def test_late_attach_rebootstraps_a_run_log_shorter_than_the_tail(tmp_path):  # noqa: ANN001, ANN201
    # The watermark check alone cannot see this attach: the whole run log fits
    # inside the tail, so ``latest_sequence - cursor`` never exceeds it. Without
    # the store's identity on the batch, the durable events at or below the
    # client's pre-attach cursor are dropped by its out-of-order guard, and the
    # floor stays 0, so backfill has no reason to fetch the prefix either.
    parts = build_server_parts(tmp_path / "server")
    tail = 1_000

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=tail)) as read:
        assert read()["type"] == "subscribed"
        bootstrap = read()
        parts.attach(_write_log(tmp_path / "logs", _round_log(200)))
        batch = read()

    latest = parts.api.snapshot().sequence
    assert latest < tail
    assert batch["store_id"] != bootstrap["store_id"]
    assert batch["history_after_sequence"] == 0
    assert batch["through_sequence"] == latest
    # A whole-log replay, so the client re-folds to exactly what the durable
    # store holds, starting at the ``run_started`` the stale cursor covered.
    assert [event["sequence"] for event in batch["events"]] == list(range(1, latest + 1))
    assert batch["events"][0]["type"] == "run_started"


def test_attach_into_an_empty_log_keeps_the_subscription_streaming(tmp_path):  # noqa: ANN001, ANN201
    # A fresh run's attach re-appends the bootstrap events into an empty log,
    # which preserves every sequence. The client's fold is still correct, so
    # the store keeps its identity and the stream stays incremental.
    parts = build_server_parts(tmp_path / "server")

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        assert read()["type"] == "subscribed"
        bootstrap = read()
        parts.attach(tmp_path / "logs")
        parts.journal.publish_output("stdout", "first line of the run")
        batch = read()

    assert batch["store_id"] == bootstrap["store_id"]
    assert [event["type"] for event in batch["events"]] == ["output"]
    assert batch["events"][0]["sequence"] == bootstrap["through_sequence"] + 1


def test_live_batches_carry_the_store_they_were_read_from(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(50))

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        read()
        bootstrap = read()
        parts.journal.publish_output("stdout", "one more line")
        batch = read()

    store_id = parts.journal.store_id_locked()
    assert store_id != ""
    assert bootstrap["store_id"] == store_id
    assert batch["store_id"] == store_id


def test_checkpoint_reads_nothing_from_a_store_the_cursor_predates(tmp_path):  # noqa: ANN001, ANN201
    parts = build_server_parts(tmp_path / "server")
    bootstrap_store = parts.journal.store_id_locked()
    cursor = parts.api.latest_sequence
    parts.attach(_write_log(tmp_path / "logs", _round_log(200)))

    stale = parts.api.subscription_checkpoint(cursor, store_id=bootstrap_store)
    current = parts.api.subscription_checkpoint(cursor)

    assert stale.store_id == parts.journal.store_id_locked() != bootstrap_store
    assert stale.events == []
    # Without the guard the same cursor reads the replacement log's suffix,
    # which is exactly the batch that used to reach the client unannounced.
    assert current.events != []


def test_late_attach_does_not_parse_skipped_history(tmp_path):  # noqa: ANN001, ANN201
    count = 8_000
    log_dir = _write_log(tmp_path / "logs", _round_log(count))
    parts = build_server_parts(tmp_path / "server")

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        read()
        read()
        parts.attach(log_dir)
        batch = read()

    store = parts.journal._store  # noqa: SLF001
    assert store is not None
    floor = batch["history_after_sequence"]
    attach_only = EventStore(log_dir / "run-events.jsonl", run_id="persisted-run")
    assert store.parsed_record_count <= (
        attach_only.parsed_record_count + 40 + _spine_records(floor) + 5
    )
    assert store.parsed_record_count < count // 2


def test_live_append_keeps_existing_tail_floor(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    floor = parts.api.snapshot().sequence - 40

    with _live_subscription(parts.api, SubscribeRequest(after_sequence=0, tail=40)) as read:
        read()
        read()
        parts.journal.publish_output("stdout", "one more line")
        batch = read()

    assert batch["history_after_sequence"] == floor
    assert [event["type"] for event in batch["events"]] == ["output"]


def test_disconnect_wait_blocks_until_last_subscriber_closes(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path / "logs")
    socket_path = Path("/tmp") / f"vibesys-test-{uuid.uuid4().hex}.sock"  # noqa: S108
    request = SubscribeRequest(after_sequence=0)

    with UnixJsonlServer(socket_path, parts.api) as server:
        with _subscribed_client(socket_path, request) as read:
            assert read()["type"] == "subscribed"
        parts.journal.publish_output("stdout", "wake the first handler")
        # With no subscriber left, the wait returns; it must not poison later waits.
        server.wait_for_subscriber_disconnect()

        with _subscribed_client(socket_path, request) as read:
            assert read()["type"] == "subscribed"
            unblocked = threading.Event()

            def wait_for_disconnect() -> None:
                server.wait_for_subscriber_disconnect()
                unblocked.set()

            waiter = threading.Thread(target=wait_for_disconnect, daemon=True)
            waiter.start()
            # The first client's earlier disconnect must not unblock the wait
            # while the second subscription is still streaming.
            assert not unblocked.wait(timeout=1.0)
        parts.journal.publish_output("stdout", "wake the second handler")

        assert unblocked.wait(timeout=5)
        waiter.join(timeout=5)
        assert not waiter.is_alive()


def test_wait_for_change_does_not_parse_events(tmp_path):  # noqa: ANN001, ANN201
    parts = _attach(tmp_path, _round_log(200))
    store = parts.journal._store  # noqa: SLF001
    assert store is not None
    parsed = store.parsed_record_count

    assert parts.api.wait_for_change(0, timeout=0.5) is True
    assert parts.api.wait_for_change(parts.api.latest_sequence, timeout=0.01) is False
    assert store.parsed_record_count == parsed
