from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Literal

import pytest

import vibesys.sandbox.skypilot_evaluator as helper_module
from vibesys.sandbox.skypilot_evaluator import run_evaluator
from vibesys.skypilot.protocol import (
    AckedFrame,
    ArtifactFrame,
    ErrorFrame,
    OutputFrame,
    ResultFrame,
    encode_message,
)

type _Frame = ArtifactFrame | ErrorFrame | OutputFrame | ResultFrame


def _serve_frames(socket_path: Path, frames: list[_Frame]) -> threading.Thread:
    """Serve one bridge conversation on ``socket_path``, then exit.

    ``ready`` is released from a ``finally`` so a server thread that dies
    during setup surfaces as a connection failure in the test rather than
    parking the main thread on ``ready.wait()`` for the life of the run.
    """
    ready = threading.Event()

    def serve() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(socket_path))
                server.listen(1)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    reader = connection.makefile("rb")
                    request = json.loads(reader.readline())
                    for frame in frames:
                        connection.sendall(encode_message(frame))
                    if any(isinstance(frame, ResultFrame) for frame in frames):
                        acknowledgement = json.loads(reader.readline())
                        assert acknowledgement["type"] == "ack"
                        connection.sendall(
                            encode_message(AckedFrame(invocation_id=request["invocation_id"]))
                        )
        finally:
            ready.set()

    thread = threading.Thread(target=serve)
    thread.start()
    ready.wait()
    return thread


@pytest.mark.parametrize(
    ("status", "expected"),
    [("COMPLETED", 0), ("APPLICATION_FAILED", 1), ("CANCELLED", 130)],
)
def test_helper_relays_streams_and_maps_terminal_status(
    socket_dir: Path,
    status: Literal["COMPLETED", "APPLICATION_FAILED", "CANCELLED"],
    expected: int,
) -> None:
    path = socket_dir / "bridge.sock"
    thread = _serve_frames(
        path,
        [
            OutputFrame(type="stdout", data="out\n"),
            OutputFrame(type="stderr", data="err\n"),
            ResultFrame(status=status, sky_exit_code=0, remote_job_id=7),
        ],
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    assert run_evaluator("accuracy", path, stdout=stdout, stderr=stderr) == expected
    thread.join()
    assert stdout.getvalue() == "out\n"
    assert stderr.getvalue() == "err\n"


def test_helper_reports_bridge_error_as_transport_failure(socket_dir: Path) -> None:
    path = socket_dir / "bridge.sock"
    thread = _serve_frames(path, [ErrorFrame(error="SkyPilotTimeoutError")])
    stderr = io.StringIO()

    assert run_evaluator("benchmark", path, stdout=io.StringIO(), stderr=stderr) == 2
    thread.join()
    assert "SkyPilotTimeoutError" in stderr.getvalue()


def test_helper_rejects_incomplete_terminal_result(socket_dir: Path) -> None:
    path = socket_dir / "bridge.sock"
    ready = threading.Event()

    def serve() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    connection.makefile("rb").readline()
                    connection.sendall(b'{"version":2,"type":"result","status":"COMPLETED"}\n')
        finally:
            ready.set()

    raw_thread = threading.Thread(target=serve)
    raw_thread.start()
    ready.wait()
    stderr = io.StringIO()

    assert run_evaluator("accuracy", path, stdout=io.StringIO(), stderr=stderr) == 2
    raw_thread.join()
    assert "invalid result" in stderr.getvalue()


def test_helper_materializes_narrow_framework_result_artifact(socket_dir: Path) -> None:
    socket_path = socket_dir / "bridge.sock"
    output_path = (
        Path(tempfile.gettempdir()) / f"vibesys-framework-benchmark-{uuid.uuid4().hex}.json"
    )
    output_path.unlink(missing_ok=True)
    thread = _serve_frames(
        socket_path,
        [
            ArtifactFrame(
                path=str(output_path),
                size=len(b'{"score": 1}'),
                sha256=hashlib.sha256(b'{"score": 1}').hexdigest(),
                data_base64=base64.b64encode(b'{"score": 1}').decode(),
            ),
            ResultFrame(status="COMPLETED", sky_exit_code=0, remote_job_id=7),
        ],
    )
    try:
        assert (
            run_evaluator(
                "benchmark",
                socket_path,
                arguments=("--output-json", str(output_path)),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            == 0
        )
        thread.join()
        assert output_path.read_text() == '{"score": 1}'
    finally:
        output_path.unlink(missing_ok=True)


def test_pending_invocation_identity_survives_helper_process_state_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_SKYPILOT_CALLER_STATE", str(tmp_path))

    first, path = helper_module._pending_invocation("accuracy", ())
    second, same_path = helper_module._pending_invocation("accuracy", ())

    assert first == second
    assert path == same_path
    assert path.is_relative_to(tmp_path)


def test_acknowledged_pending_invocation_removal_is_directory_durable(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_SKYPILOT_CALLER_STATE", str(tmp_path))
    _, pending_path = helper_module._pending_invocation("accuracy", ())
    fsynced: list[Path] = []
    monkeypatch.setattr(helper_module, "_fsync_directory", fsynced.append)
    socket_path = socket_dir / "bridge.sock"
    thread = _serve_frames(
        socket_path,
        [ResultFrame(status="COMPLETED", sky_exit_code=0, remote_job_id=7)],
    )

    assert run_evaluator("accuracy", socket_path, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    thread.join()

    assert not pending_path.exists()
    assert fsynced == [tmp_path]


def test_pending_invocation_recovers_an_incomplete_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_SKYPILOT_CALLER_STATE", str(tmp_path))
    _, path = helper_module._pending_invocation("accuracy", ())
    path.write_text("partial", encoding="utf-8")

    recovered, same_path = helper_module._pending_invocation("accuracy", ())

    assert len(recovered) == 32
    assert same_path == path
    assert path.read_text(encoding="utf-8") == recovered
