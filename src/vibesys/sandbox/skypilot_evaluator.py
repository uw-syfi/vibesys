"""Sandbox-side client for the narrow SkyPilot evaluator bridge."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TextIO

_PROTOCOL_VERSION = 2
_MAX_FRAME_BYTES = 1024 * 1024
_FRAMEWORK_ARGUMENT_COUNT = 2


def run_evaluator(  # noqa: C901, PLR0911, PLR0912, PLR0915
    kind: str,
    socket_path: Path,
    *,
    arguments: tuple[str, ...] = (),
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Request one trusted evaluator and relay its streamed output."""
    invocation_id, pending_path = _pending_invocation(kind, arguments)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        artifacts: tuple[str, ...] = ()
        if arguments:
            if (
                len(arguments) != _FRAMEWORK_ARGUMENT_COUNT
                or not arguments[0].startswith("-")
                or any(character.isspace() for character in arguments[0])
                or not arguments[1].startswith(
                    "/tmp/vibesys-framework-benchmark-"  # noqa: S108
                )
                or not arguments[1].endswith(".json")
            ):
                print("Unsupported SkyPilot evaluator arguments", file=stderr)
                return 2
            artifacts = (arguments[1],)
        request = {
            "version": _PROTOCOL_VERSION,
            "invocation_id": invocation_id,
            "kind": kind,
            "arguments": arguments,
            "artifacts": artifacts,
        }
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        reader = client.makefile("rb")
        artifact_received = False
        while payload := reader.readline(_MAX_FRAME_BYTES + 1):
            if len(payload) > _MAX_FRAME_BYTES or not payload.endswith(b"\n"):
                print("SkyPilot bridge returned an invalid frame", file=stderr)
                return 2
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                print("SkyPilot bridge returned invalid JSON", file=stderr)
                return 2
            if not isinstance(frame, dict) or frame.get("version") != _PROTOCOL_VERSION:
                print("SkyPilot bridge protocol version mismatch", file=stderr)
                return 2
            frame_type = frame.get("type")
            if (
                frame_type == "stdout"
                and set(frame) == {"version", "type", "data"}
                and isinstance(frame.get("data"), str)
            ):
                stdout.write(frame["data"])
                stdout.flush()
            elif (
                frame_type == "stderr"
                and set(frame) == {"version", "type", "data"}
                and isinstance(frame.get("data"), str)
            ):
                stderr.write(frame["data"])
                stderr.flush()
            elif frame_type == "result":
                status = frame.get("status")
                if (
                    set(frame) != {"version", "type", "status", "sky_exit_code", "remote_job_id"}
                    or status not in {"COMPLETED", "APPLICATION_FAILED", "CANCELLED"}
                    or not _strict_int(frame.get("sky_exit_code"))
                    or not _strict_int(frame.get("remote_job_id"))
                    or (status == "COMPLETED" and bool(artifacts) != artifact_received)
                ):
                    print("SkyPilot bridge returned an invalid result", file=stderr)
                    return 2
                client.sendall(
                    json.dumps(
                        {
                            "version": _PROTOCOL_VERSION,
                            "type": "ack",
                            "invocation_id": invocation_id,
                        },
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                acknowledgement = reader.readline(_MAX_FRAME_BYTES + 1)
                try:
                    acked = json.loads(acknowledgement)
                except json.JSONDecodeError:
                    print("SkyPilot bridge returned an invalid acknowledgement", file=stderr)
                    return 2
                if acked != {
                    "version": _PROTOCOL_VERSION,
                    "type": "acked",
                    "invocation_id": invocation_id,
                }:
                    print("SkyPilot bridge returned an invalid acknowledgement", file=stderr)
                    return 2
                _remove_pending_invocation(pending_path)
                return 0 if status == "COMPLETED" else 130 if status == "CANCELLED" else 1
            elif frame_type == "artifact":
                path = frame.get("path")
                data = frame.get("data_base64")
                if (
                    set(frame) != {"version", "type", "path", "size", "sha256", "data_base64"}
                    or not isinstance(path, str)
                    or path not in artifacts
                    or not isinstance(data, str)
                    or not _strict_int(frame.get("size"))
                    or not isinstance(frame.get("sha256"), str)
                    or artifact_received
                ):
                    print("SkyPilot bridge returned an invalid artifact", file=stderr)
                    return 2
                try:
                    decoded = base64.b64decode(data, validate=True)
                    valid_artifact = (
                        len(decoded) == frame["size"]
                        and hashlib.sha256(decoded).hexdigest() == frame["sha256"]
                    )
                    if not valid_artifact:
                        raise ValueError  # noqa: TRY301
                    _atomic_write(Path(path), decoded)
                except (binascii.Error, OSError, ValueError):
                    print("SkyPilot bridge returned an invalid artifact", file=stderr)
                    return 2
                artifact_received = True
            elif frame_type == "error":
                error = frame.get("error")
                message = frame.get("message")
                if (
                    set(frame) != {"version", "type", "error", "message"}
                    or not isinstance(error, str)
                    or not isinstance(message, str)
                ):
                    print("SkyPilot bridge returned an invalid frame", file=stderr)
                    return 2
                detail = f"{error}: {message}" if message else error
                print(f"SkyPilot bridge error: {detail}", file=stderr)
                return 2
            else:
                print("SkyPilot bridge returned an invalid frame", file=stderr)
                return 2
    print("SkyPilot bridge closed without a result", file=stderr)
    return 2


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pending_invocation(kind: str, arguments: tuple[str, ...]) -> tuple[str, Path]:
    """Persist the one sequential caller token across helper restarts."""
    key = hashlib.sha256(json.dumps([kind, arguments], separators=(",", ":")).encode()).hexdigest()[
        :16
    ]
    configured_root = os.environ.get("VIBESYS_SKYPILOT_CALLER_STATE")
    root = Path(configured_root) if configured_root else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"pending-{key}"
    try:
        value = path.read_text().strip()
        if re.fullmatch(r"[a-f0-9]{32}", value):
            return value, path
    except FileNotFoundError:
        pass
    else:
        path.unlink()
    value = uuid.uuid4().hex
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=root)
    temporary_path = Path(temporary)
    try:
        temporary_path.chmod(0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
        _fsync_directory(root)
    finally:
        temporary_path.unlink(missing_ok=True)
    return value, path


def _remove_pending_invocation(path: Path) -> None:
    """Durably retire an acknowledged caller token."""
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace one allowlisted artifact after full validation."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        temporary_path.chmod(0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    """Run the sandbox-side bridge client."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("kind", choices=("accuracy", "benchmark"))
    args, arguments = parser.parse_known_args()
    raise SystemExit(
        run_evaluator(
            args.kind,
            args.socket,
            arguments=tuple(arguments),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )


if __name__ == "__main__":
    main()
