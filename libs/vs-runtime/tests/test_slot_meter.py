"""Public metering properties over real durable ledgers and an injected clock."""

from __future__ import annotations

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_runtime.api import SlotLease, SlotMeter, SlotMeterError


class _Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


_OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(("open", "heartbeat", "close", "reload")),
        st.integers(min_value=0, max_value=5),
        st.integers(min_value=0, max_value=180),
    ),
    min_size=1,
    max_size=60,
)


@given(operations=_OPERATIONS)
@settings(max_examples=50)
def test_arbitrary_lease_sequences_match_occupancy_and_reload_is_idempotent(
    operations: list[tuple[str, int, int]],
) -> None:
    # Disk I/O is intentional: this property includes durable crash/reload behavior.
    with TemporaryDirectory() as directory:
        path = Path(directory) / "meter.jsonl"
        clock = _Clock()
        meter = SlotMeter(path, clock=clock)
        expected: dict[str, tuple[float, float, bool]] = {}
        for operation, identity, advance in operations:
            clock.seconds += advance
            lease_id = str(identity)
            previous = expected.get(lease_id)
            before = path.read_bytes() if path.exists() else b""
            if operation == "reload":
                meter = SlotMeter(path, clock=clock)
            elif operation == "open":
                if previous is not None:
                    with pytest.raises(SlotMeterError, match="already exists"):
                        meter.open(lease_id)
                    assert path.read_bytes() == before
                else:
                    meter.open(lease_id)
                    expected[lease_id] = (clock.seconds, clock.seconds, False)
            elif previous is None or (previous[2] and operation == "heartbeat"):
                with pytest.raises(SlotMeterError, match="is not open"):
                    getattr(meter, operation)(lease_id)
                assert (path.read_bytes() if path.exists() else b"") == before
            elif operation == "close" and previous[2]:
                meter.close(lease_id)
                assert path.read_bytes() == before
            else:
                getattr(meter, operation)(lease_id)
                expected[lease_id] = (previous[0], clock.seconds, operation == "close")

            projections = meter.leases()
            assert [
                (lease.opened_at_s, lease.accounted_until_s, lease.closed) for lease in projections
            ] == list(expected.values())
            expected_minutes = math.fsum((end - start) / 60 for start, end, _ in expected.values())
            assert meter.charged_minutes == expected_minutes
            assert all(lease.charged_minutes >= 0 for lease in projections)
            first_reload = SlotMeter(path, clock=clock)
            second_reload = SlotMeter(path, clock=clock)
            assert first_reload.leases() == second_reload.leases() == projections
            assert first_reload.charged_minutes == second_reload.charged_minutes == expected_minutes


def test_crash_between_heartbeats_charges_only_last_durable_heartbeat(tmp_path: Path) -> None:
    clock = _Clock()
    meter = SlotMeter(tmp_path / "meter.jsonl", clock=clock)
    clock.seconds = 60
    meter.open("a")
    clock.seconds = 120
    meter.heartbeat("a")
    clock.seconds = 179

    resumed = SlotMeter(tmp_path / "meter.jsonl", clock=clock)
    assert resumed.charged_minutes == 1
    assert resumed.close("a").charged_minutes == pytest.approx(119 / 60)


@given(prefix_length=st.integers(min_value=1, max_value=49))
def test_crash_during_append_recovers_only_incomplete_final_json(prefix_length: int) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "meter.jsonl"
        clock = _Clock()
        meter = SlotMeter(path, clock=clock)
        meter.open("a")
        clock.seconds = 60
        meter.heartbeat("a")
        valid = path.read_bytes()
        interrupted = b'{"kind":"close","lease_id":"a","at_s":120.0}'
        with path.open("ab") as stream:
            stream.write(interrupted[: min(prefix_length, len(interrupted) - 1)])

        resumed = SlotMeter(path, clock=clock)
        assert path.read_bytes() == valid
        assert resumed.charged_minutes == 1
        clock.seconds = 180
        assert resumed.close("a").charged_minutes == 3
        assert SlotMeter(path, clock=clock).charged_minutes == 3


@pytest.mark.parametrize("terminated", [False, True])
def test_valid_final_record_without_newline_is_retained(
    tmp_path: Path, *, terminated: bool
) -> None:
    path = tmp_path / "meter.jsonl"
    contents = b'{"kind":"open","lease_id":"a","at_s":0.0}'
    path.write_bytes(contents + (b"\n" if terminated else b""))
    clock = _Clock()
    meter = SlotMeter(path, clock=clock)
    assert len(meter.leases()) == 1
    assert path.read_bytes() == contents + b"\n"
    clock.seconds = 120
    assert meter.close("a").charged_minutes == 2


@given(
    key=st.text(min_size=1).filter(lambda key: key not in {"kind", "lease_id", "at_s"}),
    terminated=st.booleans(),
)
def test_unknown_ledger_keys_are_rejected_even_in_unterminated_tail(
    key: str, *, terminated: bool
) -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "meter.jsonl"
        contents = json.dumps({"kind": "open", "lease_id": "a", "at_s": 0.0, key: 1}).encode()
        contents += b"\n" if terminated else b""
        path.write_bytes(contents)
        with pytest.raises(SlotMeterError, match="Extra inputs are not permitted"):
            SlotMeter(path, clock=_Clock())
        assert path.read_bytes() == contents


@pytest.mark.parametrize(
    "contents",
    [
        b'{"kind":\n',
        b'{"kind":"heartbeat","lease_id":"unknown","at_s":0.0}\n',
        b'{"kind":"unexpected","lease_id":"a","at_s":0.0}\n',
        b'{"kind":"open","lease_id":"a","at_s":"0"}\n',
    ],
)
def test_complete_invalid_records_raise_without_rewriting(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "meter.jsonl"
    path.write_bytes(contents)
    with pytest.raises(SlotMeterError, match="record 1"):
        SlotMeter(path, clock=_Clock())
    assert path.read_bytes() == contents


@pytest.mark.parametrize("invalid_time", [-1.0, float("inf"), float("nan")])
def test_invalid_clock_value_does_not_change_ledger(tmp_path: Path, invalid_time: float) -> None:
    clock = _Clock()
    path = tmp_path / "meter.jsonl"
    meter = SlotMeter(path, clock=clock)
    meter.open("a")
    before = path.read_bytes()
    clock.seconds = invalid_time
    with pytest.raises(SlotMeterError, match="at_s"):
        meter.heartbeat("a")
    assert path.read_bytes() == before


def test_backwards_clock_is_rejected_across_leases_and_reload(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "meter.jsonl"
    meter = SlotMeter(path, clock=clock)
    meter.open("a")
    clock.seconds = 120
    meter.open("b")
    before = path.read_bytes()
    clock.seconds = 60
    with pytest.raises(SlotMeterError, match="at_s moved backwards"):
        SlotMeter(path, clock=clock).close("a")
    assert path.read_bytes() == before


def test_projection_rejects_negative_occupancy() -> None:
    with pytest.raises(ValidationError, match="accounted_until_s"):
        SlotLease(lease_id="a", opened_at_s=120, accounted_until_s=60, closed=False)


def test_new_nested_ledger_directory_is_persisted_and_reloadable(tmp_path: Path) -> None:
    path = tmp_path / "new" / "nested" / "ledger.jsonl"
    clock = _Clock()
    meter = SlotMeter(path, clock=clock)
    meter.open("a")
    clock.seconds = 120
    meter.close("a")
    assert path.is_file()
    assert SlotMeter(path, clock=clock).charged_minutes == 2
