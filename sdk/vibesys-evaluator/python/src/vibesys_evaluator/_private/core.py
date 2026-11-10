"""The Go core's command line (``docs/spec.md``, "Wrapper calling convention") behind one helper.

``call(group, verb, request)`` runs ``vibesys-evaluator <group> <verb>``, writes the request as
one JSON message on its stdin and decodes the one JSON message it writes to stdout;
``stream(group, verb, request, on_event)`` does the same for a long verb, which writes
``{"event": ...}`` lines before its response. Its stderr
(progress and logs) passes through to the user. Exit 0 is the response; exit 1 is a domain error
(``CoreDomainError``, from the response's ``error_kind`` and ``message``); any other exit, a
reply that is not JSON, or a missing binary is a ``CoreError`` of the core itself.

The binary is found in the wheel (``vibesys_evaluator/_bin``, where the platform wheels bundle the
Go binaries), then ``$VIBESYS_EVALUATOR_BIN``, then ``PATH``. ``loadgen`` and ``otelcapture`` are
found the same way (``find_binary(name)``), with ``$VIBESYS_EVALUATOR_LOADGEN`` and
``$VIBESYS_EVALUATOR_OTELCAPTURE``; so is ``stitch`` (the trace analyser, a Rust program the wheels
do not bundle yet), with ``$VIBESYS_EVALUATOR_STITCH``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: Names the core binary; consulted after the wheel's bundled one.
BIN_ENV = "VIBESYS_EVALUATOR_BIN"
BIN_NAME = "vibesys-evaluator"
#: The environment variable that names each of the wheel's binaries.
ENV_VARS = {
    BIN_NAME: BIN_ENV,
    "loadgen": "VIBESYS_EVALUATOR_LOADGEN",
    "otelcapture": "VIBESYS_EVALUATOR_OTELCAPTURE",
    "stitch": "VIBESYS_EVALUATOR_STITCH",
}
#: How to provide each binary that is not one of this module's Go commands.
_BUILD_HINTS = {
    "stitch": "build it from the repository's experimental/stitch (`cargo build --release -p stitch-cli`)",
}
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "_bin"
_DOMAIN_EXIT = 1


class CoreError(Exception):
    """The core itself failed: no binary, a crash, or a reply that breaks the convention."""


class CoreNotFoundError(CoreError):
    """No ``vibesys-evaluator`` binary was found."""


class CoreCrashError(CoreError):
    """The core exited with a code other than 0 and 1, or wrote a reply that is not JSON."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        """Keep the exit code (None when the reply, not the exit, broke the convention)."""
        super().__init__(message)
        self.code = code


class CoreDomainError(CoreError):
    """The operation failed on its own terms (exit 1): ``kind`` says how, ``response`` is the reply."""

    def __init__(self, kind: str, message: str, response: dict[str, Any]) -> None:
        """Keep the ``error_kind``, the message and the whole reply."""
        super().__init__(f"{kind}: {message}" if kind else message)
        self.kind = kind
        self.message = message
        self.response = response


def find_binary(name: str = BIN_NAME) -> str:
    """The binary ``name``: the wheel's own, then its environment variable, then ``PATH``.

    ``name`` is one of the wheel's binaries (``vibesys-evaluator``, ``loadgen``, ``otelcapture``)
    or ``stitch``; each has its own variable (``ENV_VARS``).
    """
    suffix = ".exe" if sys.platform == "win32" else ""
    bundled = _BUNDLED_DIR / (name + suffix)
    if bundled.is_file():
        return str(bundled)
    env = ENV_VARS[name]
    named = os.environ.get(env)
    if named:
        return named
    found = shutil.which(name)
    if found:
        return found
    build = _BUILD_HINTS.get(
        name, f"`go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/{name}@<tag>`"
    )
    if name in _BUILD_HINTS:
        msg = (
            f"the {name} binary is not bundled in this installation of vibesys-evaluator, ${env} is "
            f"unset and it is not on PATH. Set ${env} to the binary, or {build}"
        )
    else:
        msg = (
            f"the {name} binary is not bundled in this installation of vibesys-evaluator, ${env} "
            f"is unset and it is not on PATH. The platform wheels bundle it (pip install "
            f"vibesys-evaluator on Linux or macOS, x86_64 or arm64); a source distribution or "
            f"another platform does not. Set ${env} to the binary, or build it: {build}"
        )
    raise CoreNotFoundError(msg)


def call(
    group: str,
    verb: str,
    request: Mapping[str, Any] | None = None,
    *,
    timeout_secs: float | None = None,
    terminate_grace_secs: float = 0.0,
) -> dict[str, Any]:
    """Run ``vibesys-evaluator GROUP VERB`` with ``request`` and return its decoded response.

    When the wait is cut short (a signal handler raising, ``KeyboardInterrupt``), the core is killed
    at once; with ``terminate_grace_secs`` it is sent SIGTERM first and given that long to clean up
    (a verb that owns cluster state deletes it on SIGTERM), then killed.
    """
    binary = find_binary()
    body = json.dumps({} if request is None else request)
    try:
        process = subprocess.Popen(
            [binary, group, verb],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        msg = f"cannot run {BIN_NAME} at {binary!r}: {error.strerror}"
        raise CoreNotFoundError(msg) from error
    try:
        stdout, _ = process.communicate(body, timeout=timeout_secs)
    except subprocess.TimeoutExpired as error:
        _stop(process, 0.0)
        msg = f"{BIN_NAME} {group} {verb} ran past {timeout_secs}s"
        raise CoreCrashError(msg) from error
    except BaseException:
        _stop(process, terminate_grace_secs)
        raise
    return _decode(group, verb, process.returncode, stdout)


def _stop(process: subprocess.Popen[str], grace_secs: float) -> None:
    """End the child: SIGTERM and a grace period when asked, then SIGKILL."""
    if grace_secs > 0 and process.poll() is None:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_secs)
    if process.poll() is None:
        process.kill()
    process.wait()


def _decode(group: str, verb: str, code: int, stdout: str) -> dict[str, Any]:
    if code not in {0, _DOMAIN_EXIT}:
        msg = f"{BIN_NAME} {group} {verb} exited {code}"
        raise CoreCrashError(msg, code=code)
    try:
        response = json.loads(stdout)
    except ValueError as error:
        msg = f"{BIN_NAME} {group} {verb} wrote a reply that is not JSON: {stdout[:200]!r}"
        raise CoreCrashError(msg, code=code) from error
    if not isinstance(response, dict):
        msg = f"{BIN_NAME} {group} {verb} wrote a reply that is not a JSON object"
        raise CoreCrashError(msg, code=code)
    if code == _DOMAIN_EXIT:
        kind = response.get("error_kind", response.get("errorKind", ""))
        raise CoreDomainError(str(kind), str(response.get("message", "")), response)
    return response


def stream(
    group: str,
    verb: str,
    request: Mapping[str, Any] | None = None,
    *,
    on_event: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Run a long verb: each ``{"event": E}`` line it writes goes to ``on_event(E)`` as it comes.

    The last line is the response, decoded as ``call`` decodes it. The core is stopped (SIGTERM)
    when this returns early: an exception of ``on_event``, or a signal that raised here.
    """
    binary = find_binary()
    try:
        process = subprocess.Popen(
            [binary, group, verb], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
    except FileNotFoundError as error:
        msg = f"cannot run {BIN_NAME} at {binary!r}: {error.strerror}"
        raise CoreNotFoundError(msg) from error
    try:
        last = _events(process, f"{BIN_NAME} {group} {verb}", request, on_event)
        code = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
    return _reply(f"{BIN_NAME} {group} {verb}", code, last)


def _events(
    process: subprocess.Popen[str],
    what: str,
    request: Mapping[str, Any] | None,
    on_event: Callable[[dict[str, Any]], None],
) -> str:
    """Write the request, pass each event on, and return the last line that is not an event."""
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        msg = f"{what}: no pipes to the core"
        raise CoreCrashError(msg)
    stdin.write(json.dumps({} if request is None else request))
    stdin.close()
    last = ""
    for line in stdout:
        text = line.strip()
        if not text.startswith('{"event":'):
            last = text or last
            continue
        try:
            event = json.loads(text)["event"]
        except (ValueError, KeyError, TypeError) as error:
            msg = f"{what} wrote an event that is not JSON: {text[:200]!r}"
            raise CoreCrashError(msg) from error
        on_event(event)
    return last


def _reply(what: str, code: int, last: str) -> dict[str, Any]:
    """The response line of a streamed verb, or the error it carries."""
    if code not in {0, _DOMAIN_EXIT}:
        msg = f"{what} exited {code}"
        raise CoreCrashError(msg, code=code)
    try:
        response = json.loads(last)
    except ValueError as error:
        msg = f"{what} wrote a reply that is not JSON: {last[:200]!r}"
        raise CoreCrashError(msg, code=code) from error
    if not isinstance(response, dict):
        msg = f"{what} wrote a reply that is not a JSON object"
        raise CoreCrashError(msg, code=code)
    if code == _DOMAIN_EXIT:
        kind = response.get("error_kind", "")
        raise CoreDomainError(str(kind), str(response.get("message", "")), response)
    return response
