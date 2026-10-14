"""How the sandbox-side evaluator client treats every bridge reply it should not trust.

Guarantees under test, all through ``run_evaluator`` against a scripted bridge:

* A frame that is oversized, unterminated, not JSON, not an object, of another
  protocol version, of an unknown type, or carrying extra or mistyped fields is
  refused with a message naming the problem and exit status 2.
* A result is accepted only with exactly its fields, a known status, integer
  codes and the artifact the request asked for (none when none was asked for).
* An artifact is written only when its path was requested, its encoding, size and
  digest all match, it is the first one, and the target is writable; nothing is
  left behind otherwise.
* The pending token is cleared only after a matching acknowledgement.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from pathlib import Path

import pytest
from tests.support.skypilot_world import MemoryTransport, ScriptedBridge

from vs_sandbox.skypilot_evaluator import (  # test-isolation: the remote helper is a standalone script run on the cluster; its entry point is not library API.
    run_evaluator,
)

_MAX_FRAME_BYTES = 1024 * 1024
_REMOTE_ARTIFACT_PREFIX = "/tmp/vibesys-framework-benchmark-"  # noqa: S108  # lint-waiver: LW-010213 [S108]; the bridge protocol requires this fixed remote artifact path.


def _remote_artifact() -> str:
    """A fresh artifact path under the protocol's prefix, so parallel tests never share one."""
    return f"{_REMOTE_ARTIFACT_PREFIX}{uuid.uuid4().hex}.json"


def _artifact_arguments(path: str) -> tuple[str, ...]:
    return ("--output-json", path)


def _line(frame: dict[str, object]) -> bytes:
    return json.dumps({"version": 2, **frame}).encode() + b"\n"


def _result(**overrides: object) -> bytes:
    return _line(
        {
            "type": "result",
            "status": "COMPLETED",
            "sky_exit_code": 0,
            "remote_job_id": 1,
            **overrides,
        }
    )


def _artifact(path: str, data: bytes = b"payload", /, **overrides: object) -> bytes:
    return _line(
        {
            "type": "artifact",
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(data).decode(),
            **overrides,
        }
    )


def _run(
    tmp_path: Path,
    frames: list[bytes],
    arguments: tuple[str, ...] = (),
    *,
    ack_reply: bytes | None = None,
) -> tuple[int, str, ScriptedBridge]:
    transport = MemoryTransport()
    bridge = ScriptedBridge(transport, "frames.sock", frames, ack_reply=ack_reply)
    stderr = io.StringIO()
    code = run_evaluator(
        "benchmark" if arguments else "accuracy",
        Path("frames.sock"),
        arguments=arguments,
        state_dir=tmp_path,
        stdout=io.StringIO(),
        stderr=stderr,
        connect=bridge.dial,
    )
    bridge.finish()
    return code, stderr.getvalue(), bridge


@pytest.mark.parametrize(
    ("frames", "message"),
    [
        pytest.param([b'{"version":2}'], "invalid frame", id="unterminated"),
        pytest.param([b"x" * (_MAX_FRAME_BYTES + 1) + b"\n"], "invalid frame", id="oversized"),
        pytest.param([b"{not json\n"], "invalid JSON", id="not-json"),
        pytest.param([b"[1]\n"], "protocol version mismatch", id="not-an-object"),
        pytest.param([b'{"version":1}\n'], "protocol version mismatch", id="old-version"),
        pytest.param([_line({"type": "unknown"})], "invalid frame", id="unknown-type"),
        pytest.param([_line({"type": "stdout", "data": 5})], "invalid frame", id="bad-output"),
        pytest.param(
            [_line({"type": "stdout", "data": "x", "extra": 1})],
            "invalid frame",
            id="extra-output-field",
        ),
        pytest.param(
            [_line({"type": "error", "error": "boom"})], "SkyPilot bridge error: boom", id="error"
        ),
        pytest.param([_line({"type": "error", "error": 5})], "invalid frame", id="bad-error"),
        pytest.param(
            [_line({"type": "error", "error": "x", "extra": 1})],
            "invalid frame",
            id="extra-error-field",
        ),
        pytest.param([], "closed without a result", id="no-result"),
        pytest.param([_result(status="WEIRD")], "invalid result", id="unknown-status"),
        pytest.param([_result(status=3)], "invalid result", id="status-not-a-string"),
        pytest.param([_result(sky_exit_code=True)], "invalid result", id="bool-exit-code"),
        pytest.param([_result(remote_job_id="1")], "invalid result", id="string-job-id"),
        pytest.param([_result(extra=1)], "invalid result", id="extra-result-field"),
    ],
)
def test_an_untrustworthy_reply_is_a_transport_failure(
    tmp_path: Path, frames: list[bytes], message: str
) -> None:
    code, stderr, bridge = _run(tmp_path, frames)

    assert code == 2
    assert message in stderr
    assert bridge.acks == []


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
def test_an_invalid_artifact_is_refused_and_leaves_nothing(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    path = _remote_artifact()
    try:
        code, stderr, _ = _run(
            tmp_path, [_artifact(path, **overrides), _result()], _artifact_arguments(path)
        )

        assert code == 2
        assert "invalid artifact" in stderr
        assert not Path(path).exists()
    finally:
        Path(path).unlink(missing_ok=True)


def test_a_repeated_or_unwritable_artifact_is_refused(tmp_path: Path) -> None:
    path = _remote_artifact()
    try:
        code, stderr, _ = _run(
            tmp_path, [_artifact(path), _artifact(path), _result()], _artifact_arguments(path)
        )
        assert (code, "invalid artifact" in stderr) == (2, True)
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("artifact_requested", "artifact_sent"),
    [(True, False), (False, True)],
    ids=["artifact-expected-but-missing", "artifact-unrequested"],
)
def test_a_result_must_match_the_artifacts_requested(
    tmp_path: Path, *, artifact_requested: bool, artifact_sent: bool
) -> None:
    path = _remote_artifact()
    frames = [_artifact(path), _result()] if artifact_sent else [_result()]
    try:
        code, stderr, _ = _run(
            tmp_path, frames, _artifact_arguments(path) if artifact_requested else ()
        )

        assert code == 2
        assert "invalid" in stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_the_pending_token_is_cleared_only_after_a_matching_acknowledgement(
    tmp_path: Path,
) -> None:
    code, _, _ = _run(tmp_path, [_result()])
    assert code == 0
    assert list(tmp_path.glob("pending-*")) == []

    for reply in (b"garbage", _line({"type": "acked", "invocation_id": "other"})):
        code, stderr, _ = _run(tmp_path, [_result()], ack_reply=reply)

        assert code == 2
        assert "invalid acknowledgement" in stderr
        assert len(list(tmp_path.glob("pending-*"))) == 1
