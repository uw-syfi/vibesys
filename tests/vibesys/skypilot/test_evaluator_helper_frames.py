"""Frame validation in the sandbox-side SkyPilot evaluator client."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.sandbox.skypilot_evaluator import (
    _MAX_FRAME_BYTES,
    _acknowledge_result,
    _BridgeSession,
    _complete_result,
    _decode_frame,
    _process_stream_frame,
    _receive_artifact,
    _relay_output,
    _validate_result,
)

if TYPE_CHECKING:
    from pathlib import Path


def _artifact_frame(
    target: Path, data: bytes = b"payload", **overrides: object
) -> dict[str, object]:
    frame: dict[str, object] = {
        "version": 2,
        "type": "artifact",
        "path": str(target),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data_base64": base64.b64encode(data).decode(),
    }
    frame.update(overrides)
    return frame


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"version":2}', "invalid frame"),
        (b"x" * (_MAX_FRAME_BYTES + 1) + b"\n", "invalid frame"),
        (b"{not json\n", "invalid JSON"),
        (b"[1]\n", "protocol version mismatch"),
        (b'{"version":1}\n', "protocol version mismatch"),
    ],
)
def test_decode_frame_rejects_malformed_payloads(payload: bytes, message: str) -> None:
    stderr = io.StringIO()

    assert _decode_frame(payload, stderr) is None
    assert message in stderr.getvalue()


def test_decode_frame_accepts_a_versioned_object() -> None:
    assert _decode_frame(b'{"version":2,"type":"x"}\n', io.StringIO()) == {
        "version": 2,
        "type": "x",
    }


def test_relay_output_routes_streams_and_rejects_bad_frames() -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert _relay_output({"version": 2, "type": "stdout", "data": "o"}, stdout, stderr)
    assert _relay_output({"version": 2, "type": "stderr", "data": "e"}, stdout, stderr)
    assert (stdout.getvalue(), stderr.getvalue()) == ("o", "e")
    assert _relay_output({"type": "result"}, stdout, stderr) is None

    bad = {"version": 2, "type": "stdout", "data": 5}
    assert _relay_output(bad, stdout, stderr) is False
    assert _relay_output({"type": "stdout", "data": "x"}, stdout, stderr) is False
    assert stderr.getvalue().count("invalid frame") == 2
    assert stdout.getvalue() == "o"


def _result(**overrides: object) -> dict[str, object]:
    frame: dict[str, object] = {
        "version": 2,
        "type": "result",
        "status": "COMPLETED",
        "sky_exit_code": 0,
        "remote_job_id": 1,
    }
    frame.update(overrides)
    return frame


@pytest.mark.parametrize(
    ("overrides", "artifacts", "received"),
    [
        ({"status": "WEIRD"}, (), 0),
        ({"status": 3}, (), 0),
        ({"sky_exit_code": True}, (), 0),
        ({"remote_job_id": "1"}, (), 0),
        ({"extra": 1}, (), 0),
        ({}, ("a.json",), 0),
        ({}, (), 1),
    ],
)
def test_validate_result_rejects_inconsistent_frames(
    overrides: dict[str, object], artifacts: tuple[str, ...], received: int
) -> None:
    stderr = io.StringIO()

    status = _validate_result(
        _result(**overrides), artifacts, artifact_received=bool(received), stderr=stderr
    )

    assert status is None
    assert "invalid result" in stderr.getvalue()


def test_validate_result_returns_status_when_artifact_expectation_is_met() -> None:
    assert (
        _validate_result(_result(), ("a.json",), artifact_received=True, stderr=io.StringIO())
        == "COMPLETED"
    )


def test_receive_artifact_writes_a_verified_payload(tmp_path: Path) -> None:
    target = tmp_path / "out.json"

    result = _receive_artifact(
        _artifact_frame(target), (str(target),), artifact_received=False, stderr=io.StringIO()
    )

    assert result is True
    assert target.read_bytes() == b"payload"


@pytest.mark.parametrize(
    "overrides",
    [
        {"data_base64": "!!!not-base64!!!"},
        {"size": 999},
        {"sha256": "0" * 64},
        {"path": "/elsewhere"},
        {"size": True},
        {"sha256": 5},
        {"extra": 1},
    ],
)
def test_receive_artifact_rejects_invalid_frames(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    target = tmp_path / "out.json"
    stderr = io.StringIO()

    result = _receive_artifact(
        _artifact_frame(target, **overrides),
        (str(target),),
        artifact_received=False,
        stderr=stderr,
    )

    assert result is None
    assert "invalid artifact" in stderr.getvalue()
    assert not target.exists()


def test_receive_artifact_rejects_a_repeated_artifact_and_unwritable_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out.json"
    stderr = io.StringIO()
    frame = _artifact_frame(target)

    assert _receive_artifact(frame, (str(target),), artifact_received=True, stderr=stderr) is None

    missing_dir = tmp_path / "missing" / "out.json"
    assert (
        _receive_artifact(
            _artifact_frame(missing_dir),
            (str(missing_dir),),
            artifact_received=False,
            stderr=stderr,
        )
        is None
    )
    assert stderr.getvalue().count("invalid artifact") == 2


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"version": 2, "type": "error", "error": "boom"}, "SkyPilot bridge error: boom"),
        ({"version": 2, "type": "error", "error": 5}, "invalid frame"),
        ({"version": 2, "type": "error", "error": "x", "extra": 1}, "invalid frame"),
        ({"version": 2, "type": "unknown"}, "invalid frame"),
    ],
)
def test_process_stream_frame_reports_errors_and_unknown_frames(
    frame: dict[str, object], expected: str
) -> None:
    stderr = io.StringIO()

    ok = _process_stream_frame(
        frame, (), artifact_received=False, stdout=io.StringIO(), stderr=stderr
    )

    assert ok is False
    assert expected in stderr.getvalue()


def _session(tmp_path: Path, reply: bytes) -> _BridgeSession:
    return _BridgeSession(
        client=MagicMock(),
        reader=io.BytesIO(reply),
        invocation_id="abc",
        pending_path=tmp_path / "pending",
    )


def test_acknowledge_result_sends_ack_and_validates_the_reply(tmp_path: Path) -> None:
    good = json.dumps({"version": 2, "type": "acked", "invocation_id": "abc"}).encode()
    session = _session(tmp_path, good)

    assert _acknowledge_result(session) is True
    sent = session.client.sendall.call_args.args[0]  # type: ignore[attr-defined]
    assert json.loads(sent) == {"version": 2, "type": "ack", "invocation_id": "abc"}

    assert _acknowledge_result(_session(tmp_path, b"not json")) is False
    wrong = json.dumps({"version": 2, "type": "acked", "invocation_id": "other"}).encode()
    assert _acknowledge_result(_session(tmp_path, wrong)) is False


@pytest.mark.parametrize(
    ("status", "code"), [("COMPLETED", 0), ("APPLICATION_FAILED", 1), ("CANCELLED", 130)]
)
def test_complete_result_maps_status_and_clears_pending_marker(
    tmp_path: Path, status: str, code: int
) -> None:
    reply = json.dumps({"version": 2, "type": "acked", "invocation_id": "abc"}).encode()
    session = _session(tmp_path, reply)
    session.pending_path.write_text("abc")

    exit_code = _complete_result(
        _result(status=status), (), artifact_received=False, session=session, stderr=io.StringIO()
    )

    assert exit_code == code
    assert not session.pending_path.exists()


def test_complete_result_fails_on_bad_acknowledgement_and_keeps_marker(tmp_path: Path) -> None:
    session = _session(tmp_path, b"garbage")
    session.pending_path.write_text("abc")
    stderr = io.StringIO()

    exit_code = _complete_result(
        _result(), (), artifact_received=False, session=session, stderr=stderr
    )

    assert exit_code == 2
    assert "invalid acknowledgement" in stderr.getvalue()
    assert session.pending_path.exists()
