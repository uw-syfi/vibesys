"""The Go core's command line (``docs/spec.md``, "Wrapper calling convention") behind one helper.

``call(group, verb, request)`` runs ``vibesys-evaluator <group> <verb>``, writes the request as
one JSON message on its stdin and decodes the one JSON message it writes to stdout. Its stderr
(progress and logs) passes through to the user. Exit 0 is the response; exit 1 is a domain error
(``CoreDomainError``, from the response's ``error_kind`` and ``message``); any other exit, a
reply that is not JSON, or a missing binary is a ``CoreError`` of the core itself.

The binary is found in the wheel (``vibesys_evaluator/_bin``), then ``$VIBESYS_EVALUATOR_BIN``,
then ``PATH``.
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


def find_binary() -> str:
    """The core binary: the wheel's own, then ``$VIBESYS_EVALUATOR_BIN``, then ``PATH``."""
    suffix = ".exe" if sys.platform == "win32" else ""
    bundled = _BUNDLED_DIR / (BIN_NAME + suffix)
    if bundled.is_file():
        return str(bundled)
    named = os.environ.get(BIN_ENV)
    if named:
        return named
    found = shutil.which(BIN_NAME)
    if found:
        return found
    msg = (
        f"the {BIN_NAME} binary is not bundled, ${BIN_ENV} is unset and it is not on PATH; install it "
        f"with `go install github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/cmd/{BIN_NAME}@<tag>`"
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
