"""Registering compute backends must stay side-effect free.

``backends.get`` runs on the startup path of every entry point. Importing
``vs_sandbox.docker_sandbox`` installs a process-wide SIGINT handler and an
``atexit`` hook, so the backend modules defer it until a Docker sandbox is
actually constructed.

The probe runs in a subprocess because import side effects are a property of a
fresh interpreter, and the pytest session has usually imported the module.
"""

import subprocess
import sys
from pathlib import Path

from vibesys import backends
from vibesys.backends import SandboxKind
from vibesys.backends.local import cpu_backend
from vibesys.constants import ComputeBackend
from vs_sandbox import LocalShellSandbox

_PROBE = """
import sys

from vibesys import backends
from vibesys.constants import ComputeBackend

backends.get(ComputeBackend.{backend}, log_dir={log_dir!r})
print("vs_sandbox.docker_sandbox" in sys.modules)
"""


def _docker_sandbox_loaded_after_backend_construction(backend: str, log_dir: Path) -> bool:
    result = subprocess.run(  # noqa: S603  # tracked: #288
        [sys.executable, "-c", _PROBE.format(backend=backend, log_dir=str(log_dir))],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "True"


def test_constructing_a_compute_backend_does_not_import_docker_sandbox(tmp_path: Path) -> None:
    """Backend construction stays side-effect free for every registered backend.

    Registration imports all backend modules, so one backend is enough to catch
    a module-level ``docker_sandbox`` import in any of them.
    """
    assert not _docker_sandbox_loaded_after_backend_construction("CUDA", tmp_path)


def test_local_sandbox_construction_builds_a_local_shell_sandbox(tmp_path: Path) -> None:
    """The local sandbox kind resolves to the first-party local shell."""
    sandbox = cpu_backend(log_dir=tmp_path).make_sandbox(
        SandboxKind.LOCAL,
        host_workspace=str(tmp_path),
        log_path=None,
    )

    assert isinstance(sandbox, LocalShellSandbox)


def test_backend_registry_still_resolves_every_default(tmp_path: Path) -> None:
    """Deferring registration must not lose a backend."""
    for backend in (
        ComputeBackend.CPU,
        ComputeBackend.METAL,
        ComputeBackend.CUDA,
        ComputeBackend.ROCM,
        ComputeBackend.TRAINIUM,
    ):
        assert backends.get(backend, log_dir=tmp_path) is not None
