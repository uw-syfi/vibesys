"""Importing the Docker backend must not change process-wide interpreter state."""

import sys

from tests.support import run_test_command

_PROBE = """
import importlib
import signal

before = signal.getsignal(signal.SIGINT)
importlib.import_module("vs_sandbox.docker_sandbox")
print(signal.getsignal(signal.SIGINT) is before)
"""


def test_importing_the_docker_backend_leaves_the_sigint_handler_unchanged() -> None:
    """A fresh interpreter keeps its SIGINT disposition across the import.

    The probe runs in a subprocess because the pytest session has usually
    imported the module already, which would hide an import-time side effect.
    """
    result = run_test_command(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == "True"
