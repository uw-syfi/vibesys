"""The sandbox-side evaluator helper, driven against a scripted in-memory bridge.

Guarantees under test (``vs_sandbox.skypilot_evaluator.run_evaluator``):

* Output frames reach the caller's stdout and stderr unchanged, and the terminal
  status maps to the process exit status (completed 0, application failure 1,
  cancelled 130).
* Any bridge error, malformed or incomplete result is a transport failure (exit
  status 2) that names the cause on stderr, and never a success.
* A framework result artifact is written atomically to the one allowlisted path
  and only when its size and digest match.
* The pending invocation token survives helper restarts until a result is
  acknowledged, is replaced when its file is incomplete, and its removal is
  durable on the directory.
* The command line refuses to run without an explicit caller state directory.

The bridge is a ``ScriptedBridge`` over an in-memory network, so no test touches a
socket file or depends on the host's temporary directory.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import vs_sandbox.skypilot_evaluator as helper_module  # test-isolation: failure injection exercises the private remote helper process boundary.
from vs_sandbox.api import STEP_TAG_ENV
from vs_sandbox.api.skypilot import (
    ArtifactFrame,
    ErrorFrame,
    JobStatus,
    OutputFrame,
    ResultFrame,
    encode_message,
)
from vs_sandbox.skypilot_evaluator import (  # test-isolation: the remote helper is a standalone script run on the cluster; its entry point is not library API.
    run_evaluator,
)

from .skypilot_world import MemoryTransport, ScriptedBridge

# The scripted bridge runs on a thread: an exception there must fail the test, not warn.
pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")

if TYPE_CHECKING:
    from vs_sandbox.api.skypilot import ResponseFrame

_SOCKET = Path("bridge.sock")
_REMOTE_ARTIFACT_PREFIX = "/tmp/vibesys-framework-benchmark-"  # noqa: S108  # lint-waiver: LW-010299 [S108]; the bridge protocol requires this fixed remote artifact path.
_COMPLETED = ResultFrame(status=JobStatus.COMPLETED, sky_exit_code=0, remote_job_id=7)


def _bridge(
    transport: MemoryTransport, frames: list[ResponseFrame] | list[bytes]
) -> ScriptedBridge:
    encoded = [frame if isinstance(frame, bytes) else encode_message(frame) for frame in frames]
    return ScriptedBridge(transport, str(_SOCKET), encoded)


def _run(
    bridge: ScriptedBridge,
    state_dir: Path,
    kind: str = "accuracy",
    arguments: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    status = run_evaluator(
        kind,
        _SOCKET,
        arguments=arguments,
        state_dir=state_dir,
        stdout=stdout,
        stderr=stderr,
        connect=bridge.dial,
    )
    bridge.finish()
    return status, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(JobStatus.COMPLETED, 0), (JobStatus.APPLICATION_FAILED, 1), (JobStatus.CANCELLED, 130)],
)
def test_helper_relays_streams_and_maps_terminal_status(
    tmp_path: Path, status: JobStatus, expected: int
) -> None:
    bridge = _bridge(
        MemoryTransport(),
        [
            OutputFrame(type="stdout", data="out\n"),
            OutputFrame(type="stderr", data="err\n"),
            ResultFrame(status=status, sky_exit_code=0, remote_job_id=7),
        ],
    )

    code, out, err = _run(bridge, tmp_path)

    assert (code, out, err) == (expected, "out\n", "err\n")
    assert len(bridge.acks) == 1


def test_helper_reports_bridge_error_as_transport_failure(tmp_path: Path) -> None:
    bridge = _bridge(MemoryTransport(), [ErrorFrame(error="SkyPilotTimeoutError")])

    code, _, err = _run(bridge, tmp_path, "benchmark")

    assert code == 2
    assert "SkyPilotTimeoutError" in err
    assert bridge.acks == []


def test_helper_rejects_incomplete_terminal_result(tmp_path: Path) -> None:
    bridge = _bridge(MemoryTransport(), [b'{"version":2,"type":"result","status":"COMPLETED"}\n'])

    code, _, err = _run(bridge, tmp_path)

    assert code == 2
    assert "invalid result" in err


def test_helper_materializes_narrow_framework_result_artifact(tmp_path: Path) -> None:
    payload = b'{"score": 1}'
    # The protocol fixes the remote directory at /tmp, so the artifact is written there
    # under a unique name and removed afterwards.
    output_path = f"{_REMOTE_ARTIFACT_PREFIX}{uuid.uuid4().hex}.json"
    bridge = _bridge(
        MemoryTransport(),
        [
            ArtifactFrame(
                path=output_path,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                data_base64=base64.b64encode(payload).decode(),
            ),
            _COMPLETED,
        ],
    )
    try:
        code, _, _ = _run(bridge, tmp_path, "benchmark", ("--output-json", output_path))

        assert code == 0
        assert Path(output_path).read_bytes() == payload
        assert bridge.requests[0]["artifacts"] == [output_path]
    finally:
        Path(output_path).unlink(missing_ok=True)


def test_pending_invocation_identity_survives_helper_process_state_reload(tmp_path: Path) -> None:
    transport = MemoryTransport()
    first = _bridge(transport, [])
    assert _run(first, tmp_path)[0] == 2
    pending_files = list(tmp_path.glob("pending-*"))
    assert len(pending_files) == 1

    second = _bridge(transport, [_COMPLETED])
    assert _run(second, tmp_path)[0] == 0

    assert first.requests[0]["invocation_id"] == second.requests[0]["invocation_id"]
    assert not pending_files[0].exists()


def test_acknowledged_pending_invocation_removal_is_directory_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced: list[Path] = []
    original_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fsynced.append(Path(f"/proc/self/fd/{descriptor}").readlink())
        original_fsync(descriptor)

    monkeypatch.setattr(helper_module.os, "fsync", track_fsync)
    bridge = _bridge(MemoryTransport(), [_COMPLETED])

    assert _run(bridge, tmp_path)[0] == 0

    assert list(tmp_path.glob("pending-*")) == []
    assert fsynced == [tmp_path, tmp_path]


def test_pending_invocation_recovers_an_incomplete_token_file(tmp_path: Path) -> None:
    transport = MemoryTransport()
    first = _bridge(transport, [])
    assert _run(first, tmp_path)[0] == 2
    pending_path = next(tmp_path.glob("pending-*"))
    pending_path.write_text("partial", encoding="utf-8")

    recovered = _bridge(transport, [_COMPLETED])
    assert _run(recovered, tmp_path)[0] == 0

    recovered_id = recovered.requests[0]["invocation_id"]
    assert isinstance(recovered_id, str)
    assert len(recovered_id) == 32
    assert recovered_id != first.requests[0]["invocation_id"]
    assert not pending_path.exists()


def test_helper_cli_requires_an_explicit_state_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The helper must not fall back to a predictable name in the shared temp root."""
    with pytest.raises(SystemExit) as exit_info:
        helper_module.main(["--socket", str(tmp_path / "x.sock"), "accuracy"], environ={})

    assert exit_info.value.code == 2
    assert "VIBESYS_SKYPILOT_CALLER_STATE" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("step_tag", ["step-a", None])
def test_the_helper_sends_the_step_mark_its_turn_started_it_with(
    tmp_path: Path, step_tag: str | None
) -> None:
    bridge = _bridge(MemoryTransport(), [_COMPLETED])
    run_evaluator(
        "accuracy",
        _SOCKET,
        state_dir=tmp_path,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        step_tag=step_tag,
        connect=bridge.dial,
    )
    bridge.finish()

    (request,) = bridge.requests
    assert request.get("step_tag") == step_tag
    assert ("step_tag" in request) == (step_tag is not None)


def test_the_helper_names_the_mark_the_framework_sets() -> None:
    """The helper is mounted alone, so it restates the mark's name; this pins the two together."""
    assert helper_module._STEP_TAG_ENV == STEP_TAG_ENV  # noqa: SLF001  # test-isolation: the standalone helper's constant is not library API.
