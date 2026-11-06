"""The Go core's command line (``docs/spec.md``, "Wrapper calling convention") behind one helper.

``call(group, verb, request)`` runs ``vibesys-evaluator <group> <verb>``, writes the request as
one JSON message on its stdin and decodes the one JSON message it writes to stdout. Its stderr
(progress and logs) passes through to the user. Exit 0 is the response; exit 1 is a domain error
(``CoreDomainError``, from the response's ``error_kind`` and ``message``); any other exit, a
reply that is not JSON, or a missing binary is a ``CoreError`` of the core itself.

The binary is found in the wheel (``vibesys_evaluator/_bin``, where the platform wheels bundle the
Go binaries), then ``$VIBESYS_EVALUATOR_BIN``, then ``PATH``. ``loadgen`` and ``otelcapture`` are
found the same way (``find_binary(name)``), with ``$VIBESYS_EVALUATOR_LOADGEN`` and
``$VIBESYS_EVALUATOR_OTELCAPTURE``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Names the core binary; consulted after the wheel's bundled one.
BIN_ENV = "VIBESYS_EVALUATOR_BIN"
BIN_NAME = "vibesys-evaluator"
#: The environment variable that names each of the wheel's binaries.
ENV_VARS = {
    BIN_NAME: BIN_ENV,
    "loadgen": "VIBESYS_EVALUATOR_LOADGEN",
    "otelcapture": "VIBESYS_EVALUATOR_OTELCAPTURE",
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

    ``name`` is one of the wheel's binaries (``vibesys-evaluator``, ``loadgen``, ``otelcapture``);
    each has its own variable (``ENV_VARS``).
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
    msg = (
        f"the {name} binary is not bundled in this installation of vibesys-evaluator, ${env} is "
        f"unset and it is not on PATH. The platform wheels bundle it (pip install vibesys-evaluator "
        f"on Linux or macOS, x86_64 or arm64); a source distribution or another platform does not. "
        f"Set ${env} to the binary, or build it: "
        f"`go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/{name}@<tag>`"
    )
    raise CoreNotFoundError(msg)


def call(
    group: str,
    verb: str,
    request: Mapping[str, Any] | None = None,
    *,
    timeout_secs: float | None = None,
) -> dict[str, Any]:
    """Run ``vibesys-evaluator GROUP VERB`` with ``request`` and return its decoded response."""
    binary = find_binary()
    body = json.dumps({} if request is None else request)
    try:
        done = subprocess.run(
            [binary, group, verb],
            input=body,
            stdout=subprocess.PIPE,
            text=True,
            timeout=timeout_secs,
            check=False,
        )
    except FileNotFoundError as error:
        msg = f"cannot run {BIN_NAME} at {binary!r}: {error.strerror}"
        raise CoreNotFoundError(msg) from error
    except subprocess.TimeoutExpired as error:
        msg = f"{BIN_NAME} {group} {verb} ran past {timeout_secs}s"
        raise CoreCrashError(msg) from error
    if done.returncode not in {0, _DOMAIN_EXIT}:
        msg = f"{BIN_NAME} {group} {verb} exited {done.returncode}"
        raise CoreCrashError(msg, code=done.returncode)
    try:
        response = json.loads(done.stdout)
    except ValueError as error:
        msg = f"{BIN_NAME} {group} {verb} wrote a reply that is not JSON: {done.stdout[:200]!r}"
        raise CoreCrashError(msg, code=done.returncode) from error
    if not isinstance(response, dict):
        msg = f"{BIN_NAME} {group} {verb} wrote a reply that is not a JSON object"
        raise CoreCrashError(msg, code=done.returncode)
    if done.returncode == _DOMAIN_EXIT:
        kind = response.get("error_kind", response.get("errorKind", ""))
        raise CoreDomainError(str(kind), str(response.get("message", "")), response)
    return response
