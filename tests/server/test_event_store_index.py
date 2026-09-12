"""Disposable-index and streaming-attach tests for the event store."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import server.event_index as event_index_module
import server.events as events_module
from server.event_index import event_index_path, load_event_index
from server.events import EventStore, EventType, RunEvent, make_event

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

type _ScannedHeader = tuple[int, EventType, str | None, str | None]
type _SidecarMutation = Callable[[dict[str, object], list[list[object]]], None]

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _event(sequence: int, text: str) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        run_id="persisted-run",
        timestamp=_TIMESTAMP,
        type=EventType.OUTPUT,
        text=text,
    )


def _write(path: Path, sequences: list[int], *, terminate: bool = True) -> None:
    content = "\n".join(
        _event(sequence, f"event-{index}").model_dump_json()
        for index, sequence in enumerate(sequences)
    )
    path.write_text(content + ("\n" if terminate else ""))


def _rewrite_sidecar(path: Path, mutate: _SidecarMutation) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:-1]]
    mutate(header, records)
    encoded = [
        (json.dumps(header, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        *[
            (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
            for record in records
        ],
    ]
    digest = hashlib.sha256(b"".join(encoded), usedforsecurity=False).hexdigest()
    path.write_bytes(b"".join(encoded) + f'{{"sha256":"{digest}"}}\n'.encode())


def test_cold_attach_never_reads_the_complete_file_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, list(range(1, 1_500)))

    def reject_read_bytes(_path: Path) -> None:
        raise AssertionError

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    store = EventStore(path, run_id="active-run")

    assert len(store.event_headers()) == 1_499
    assert store.last_sequence == 1_499


def test_a_valid_record_rejected_by_the_header_scan_is_validated_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    encoded = _event(1, "coerced").model_dump_json().replace('"sequence":1', '"sequence":"1"')
    path.write_text(encoded + "\n")

    def reject_read_bytes(_path: Path) -> None:
        raise AssertionError

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    store = EventStore(path, run_id="active-run")

    assert [(event.sequence, event.text) for event in store.read()] == [(1, "coerced")]
    assert load_event_index(path) is not None


def test_warm_attach_uses_the_validated_sidecar_without_scanning_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, list(range(1, 1_500)))
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    original_index = index_path.read_bytes()

    def reject_header_scan(_line: bytes) -> _ScannedHeader | None:
        raise AssertionError

    monkeypatch.setattr(events_module, "_scan_header_fields", reject_header_scan)

    store = EventStore(path, run_id="second")

    assert store.last_sequence == 1_499
    assert index_path.read_bytes() == original_index


def test_appended_source_rebuilds_instead_of_trusting_a_stale_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2, 3])
    EventStore(path, run_id="first")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_event(4, "appended").model_dump_json() + "\n")
    scanned = 0
    scan_header = events_module._scan_header_fields  # noqa: SLF001

    def count_header_scans(line: bytes) -> _ScannedHeader | None:
        nonlocal scanned
        scanned += 1
        return scan_header(line)

    monkeypatch.setattr(events_module, "_scan_header_fields", count_header_scans)

    store = EventStore(path, run_id="second")

    assert scanned == 4
    assert [event.text for event in store.read()] == ["event-0", "event-1", "event-2", "appended"]


def test_source_change_during_scan_does_not_publish_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    scan_header = events_module._scan_header_fields  # noqa: SLF001
    changed = False

    def change_during_scan(line: bytes) -> _ScannedHeader | None:
        nonlocal changed
        if not changed:
            changed = True
            with path.open("a", encoding="utf-8") as stream:
                stream.write(_event(3, "concurrent").model_dump_json() + "\n")
        return scan_header(line)

    monkeypatch.setattr(events_module, "_scan_header_fields", change_during_scan)

    store = EventStore(path, run_id="active-run")

    assert store.last_sequence in {2, 3}
    assert not event_index_path(path).exists()


def test_source_change_while_loading_sidecar_rejects_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    EventStore(path, run_id="first")
    validate_footer = event_index_module._valid_footer  # noqa: SLF001
    changed = False

    def change_after_validation(stream: BinaryIO, digest: event_index_module._Digest) -> bool:
        nonlocal changed
        valid = validate_footer(stream, digest)
        if not changed:
            changed = True
            with path.open("a", encoding="utf-8") as output:
                output.write(_event(3, "concurrent").model_dump_json() + "\n")
        return valid

    monkeypatch.setattr(event_index_module, "_valid_footer", change_after_validation)

    assert [event.text for event in EventStore(path, run_id="second").read()] == [
        "event-0",
        "event-1",
        "concurrent",
    ]


def test_boundary_fingerprint_rejects_same_size_source_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2, 3])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    original_stat = path.stat()
    content = path.read_text()
    path.write_text(content.replace("event-2", "edited2"))
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    current_stat = path.stat()

    def copy_current_stat(header: dict[str, object], _records: list[list[object]]) -> None:
        source = header["source"]
        assert isinstance(source, dict)
        source["device"] = current_stat.st_dev
        source["inode"] = current_stat.st_ino
        source["size"] = current_stat.st_size
        source["mtime_ns"] = current_stat.st_mtime_ns
        source["ctime_ns"] = current_stat.st_ctime_ns

    # Make every stat field look current and repair the sidecar checksum. The
    # stale content fingerprint must still force a source scan.
    _rewrite_sidecar(index_path, copy_current_stat)
    scanned = 0
    scan_header = events_module._scan_header_fields  # noqa: SLF001

    def count_header_scans(line: bytes) -> _ScannedHeader | None:
        nonlocal scanned
        scanned += 1
        return scan_header(line)

    monkeypatch.setattr(events_module, "_scan_header_fields", count_header_scans)

    store = EventStore(path, run_id="second")

    assert scanned == 3
    assert store.read()[-1].text == "edited2"


def test_source_truncated_below_the_cached_boundary_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2, 3])
    EventStore(path, run_id="first")
    _write(path, [1, 2])

    store = EventStore(path, run_id="second")

    assert [event.sequence for event in store.read()] == [1, 2]
    loaded = load_event_index(path)
    assert loaded is not None
    assert len(loaded.records) == 2


@pytest.mark.parametrize("corruption", [b"not-json\n", b""])
def test_corrupt_or_truncated_sidecar_is_rebuilt(tmp_path: Path, corruption: bytes) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2, 3])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    index_path.write_bytes(corruption)

    store = EventStore(path, run_id="second")

    assert [event.sequence for event in store.read()] == [1, 2, 3]
    assert load_event_index(path) is not None


def test_unknown_cached_event_type_is_rebuilt_from_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)

    def replace_type(_header: dict[str, object], records: list[list[object]]) -> None:
        records[0][4] = "future_event"

    _rewrite_sidecar(index_path, replace_type)

    store = EventStore(path, run_id="second")

    assert [event.type for event in store.read()] == [EventType.OUTPUT, EventType.OUTPUT]
    assert load_event_index(path) is not None


def test_non_ascii_sidecar_checksum_is_a_cache_miss(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    lines = index_path.read_bytes().splitlines(keepends=True)
    lines[-1] = (json.dumps({"sha256": "é" * 64}) + "\n").encode()
    index_path.write_bytes(b"".join(lines))

    assert [event.sequence for event in EventStore(path, run_id="second").read()] == [1, 2]
    assert load_event_index(path) is not None


def test_valid_unterminated_record_stays_outside_the_safe_cache_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2], terminate=False)
    EventStore(path, run_id="first")
    loaded = load_event_index(path)
    assert loaded is not None
    assert len(loaded.records) == 1
    scanned = 0
    scan_header = events_module._scan_header_fields  # noqa: SLF001

    def count_header_scans(line: bytes) -> _ScannedHeader | None:
        nonlocal scanned
        scanned += 1
        return scan_header(line)

    monkeypatch.setattr(events_module, "_scan_header_fields", count_header_scans)
    store = EventStore(path, run_id="second")

    assert scanned == 1
    store.append(make_event(EventType.OUTPUT, "third"))
    assert [event.text for event in EventStore(path, run_id="third").read()] == [
        "event-0",
        "event-1",
        "third",
    ]


def test_malformed_tail_stays_repairable_after_warm_attach(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"protocol_version":1')
    first = EventStore(path, run_id="first")
    assert [event.text for event in first.read()] == ["event-0"]

    second = EventStore(path, run_id="second")
    second.append(make_event(EventType.OUTPUT, "after repair"))

    assert [event.text for event in EventStore(path, run_id="third").read()] == [
        "event-0",
        "after repair",
    ]


def test_legacy_sequence_repair_survives_cache_reuse_and_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [2, 1, 3])

    assert [event.sequence for event in EventStore(path, run_id="first").read()] == [2, 3, 4]
    warm = EventStore(path, run_id="second")
    assert [event.sequence for event in warm.read()] == [2, 3, 4]
    warm.append(make_event(EventType.OUTPUT, "appended"))

    assert [event.sequence for event in EventStore(path, run_id="third").read()] == [2, 3, 4, 5]


def test_sidecar_write_failure_never_fails_attach_or_leaves_a_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError

    monkeypatch.setattr(event_index_module.os, "replace", fail_replace)

    store = EventStore(path, run_id="active-run")

    assert [event.sequence for event in store.read()] == [1, 2]
    assert not event_index_path(path).exists()
    assert list(tmp_path.glob(".events.jsonl.idx.*")) == []


def test_temporary_cleanup_failure_does_not_mask_a_sidecar_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1])
    real_unlink = Path.unlink

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError

    def fail_temporary_unlink(target: Path, *, missing_ok: bool = False) -> None:
        if target.name.startswith(".events.jsonl.idx."):
            raise OSError
        return real_unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(event_index_module.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    assert [event.sequence for event in EventStore(path, run_id="active-run").read()] == [1]


def test_failed_replacement_preserves_an_existing_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    original = index_path.read_bytes()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError

    monkeypatch.setattr(event_index_module.os, "replace", fail_replace)

    store = EventStore(path, run_id="second")

    assert [event.sequence for event in store.read()] == [1, 2]
    assert index_path.read_bytes() == original
    assert list(tmp_path.glob(".events.jsonl.idx.*")) == []


def test_unreadable_sidecar_is_only_a_cache_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    _write(path, [1, 2])
    EventStore(path, run_id="first")
    index_path = event_index_path(path)
    real_open = Path.open

    def deny_index_read(  # noqa: PLR0913  # mirrors Path.open for the monkeypatch
        target: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if target == index_path and mode == "rb":
            raise PermissionError
        return real_open(target, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", deny_index_read)

    assert [event.sequence for event in EventStore(path, run_id="second").read()] == [1, 2]
