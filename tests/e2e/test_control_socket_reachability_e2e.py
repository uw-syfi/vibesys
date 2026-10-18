"""Whether a confined agent can connect to its run's control socket, per backend (#3133).

A run's server listens on ``control.sock`` in the user's runtime directory (the path
``server.registry.instance_socket_path`` derives). Whoever connects can send any operator
control, including a cancel of another attempt. This suite binds a real listening socket at
that path and runs a probe under each confinement backend, built the way a run builds it,
that tries to connect.

The expected results pin today's behavior. Bubblewrap and Docker do not show the runtime
directory to the agent, so the connection fails. Landlock has no right that governs
connecting to a pathname socket, and Seatbelt's profile allows ``network*``, so the probe
connects there. Those two cases are strict ``xfail`` against #3133: the fix makes them
pass, and strictness then fails the suite until the marker is removed.

Bubblewrap, Landlock and Seatbelt skip on a host without them (CI's ``sandbox_tests``
group sets ``VIBESYS_REQUIRE_SANDBOX_TESTS=1`` and gets a failure instead). Docker is
skipped unless ``VIBESYS_E2E_DOCKER=1``:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_control_socket_reachability_e2e.py -q
```
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest
from tests.support import run_test_command

from server.registry import (
    instance_root,
    instance_run_directory,
    instance_socket_path,
    new_instance_id,
)
from vibesys.run.environment import open_run_environment
from vs_runtime.api.infrastructure import (
    EnvironmentSystem,
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
)
from vs_sandbox import host_sandbox, landlock
from vs_sandbox.api import (
    ComputeBackend,
    ProjectPathPolicy,
    SandboxUnavailableError,
    SubprocessDockerCli,
)
from vs_sandbox.host_sandbox import HostSandbox, LandlockSandbox, LinuxBackend, SeatbeltSandbox
from vs_sandbox.local_compute_backend import LocalBackend
from vs_sim.api import (
    DISCARD,
    MonotonicClock,
    OsProcesses,
    OsThreads,
    SubprocessPipedProcessLauncher,
    UnixNetwork,
)
from vs_sim.api.testing import HANG_GUARD_S, NO_PROCESSES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_sandbox.host_sandbox import WorkspaceSandbox

pytestmark = pytest.mark.e2e

_REQUIRED = os.environ.get("VIBESYS_REQUIRE_SANDBOX_TESTS") == "1"
_DOCKER_ENV = "VIBESYS_E2E_DOCKER"
_BASE_IMAGE = os.environ.get("VIBESYS_E2E_DOCKER_BASE_IMAGE", "python:3.12-bookworm")
_ENV = {"HOME": "/home/socket-probe", "PATH": "/usr/bin:/bin"}

#: Prints ``connected`` or ``refused <errno name>``; any other output means the probe
#: itself did not run, which must not pass for a refusal.
_PROBE = (
    "import errno, socket, sys\n"
    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "try:\n"
    "    s.connect(sys.argv[1])\n"
    "except OSError as e:\n"
    "    print('refused', errno.errorcode.get(e.errno, e.errno))\n"
    "else:\n"
    "    print('connected')\n"
)

#: Only the reachability assertion may fail: a probe that did not run fails the test
#: through ``pytest.fail``, which this marker does not excuse.
_OPEN_ISSUE = pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="#3133: an agent under this backend can connect to its run's control socket",
)


def _absent(reason: str) -> NoReturn:
    """Skip, or fail where CI requires the real confinement backends."""
    if _REQUIRED:
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
def control_socket() -> Iterator[Path]:
    """A listening socket where a run's server binds its control socket."""
    root = instance_root(os.environ, os.getuid())
    instance = new_instance_id()
    directory = instance_run_directory(root, instance)
    directory.mkdir(mode=0o700, parents=True)
    path = instance_socket_path(root, instance)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The kernel queues a probe's connection; nothing here accepts, so nothing blocks.
    listener.settimeout(HANG_GUARD_S)
    try:
        listener.bind(str(path))
        listener.listen(4)
        yield path
    finally:
        listener.close()
        path.unlink(missing_ok=True)
        directory.rmdir()


@pytest.fixture
def workspace() -> Iterator[Path]:
    """A project directory outside ``/tmp``: Landlock refuses a project under a granted tree.

    The test session moves ``HOME`` under ``/tmp``, so the account's own home is read
    from the password database.
    """
    parent = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="vibesys-socket-probe-") as root:
        project = Path(root) / "project"
        project.mkdir()
        yield project


def _bubblewrap(workspace: Path) -> WorkspaceSandbox:
    if not sys.platform.startswith("linux"):
        pytest.skip("bubblewrap is Linux only")
    try:
        sandbox = host_sandbox.build(
            workspace, env=dict(_ENV), resources=(), require_enforcement=True
        )
    except SandboxUnavailableError as error:
        _absent(f"requires working bubblewrap: {error}")
    if not isinstance(sandbox, HostSandbox):
        _absent(f"bubblewrap was not selected: {type(sandbox).__name__}")
    return sandbox


def _landlock(workspace: Path) -> WorkspaceSandbox:
    if not sys.platform.startswith("linux"):
        pytest.skip("Landlock is Linux only")
    if landlock.abi_version() is None:
        _absent("requires a kernel with Landlock support")
    sandbox = host_sandbox.build(
        workspace,
        env={**_ENV, host_sandbox.DISABLE_ENV: LinuxBackend.LANDLOCK.value},
        resources=(),
        require_enforcement=True,
    )
    assert isinstance(sandbox, LandlockSandbox)
    return sandbox


def _seatbelt(workspace: Path) -> WorkspaceSandbox:
    if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        pytest.skip("requires macOS with sandbox-exec")
    sandbox = host_sandbox.build(workspace, env=dict(_ENV), resources=(), require_enforcement=True)
    assert isinstance(sandbox, SeatbeltSandbox)
    return sandbox


#: The interpreter the probe runs under: the system one on Linux, which every backend
#: lets an agent read; on macOS the test's own, since Seatbelt reads broadly.
_INTERPRETER = "/usr/bin/python3" if sys.platform.startswith("linux") else sys.executable


def _connects(output: str) -> bool:
    """Whether the probe connected; fails the test if the probe did not run."""
    lines = output.strip().splitlines()
    verdict = lines[-1] if lines else ""
    if verdict != "connected" and not verdict.startswith("refused "):
        pytest.fail(f"the probe did not run: {output!r}")
    return verdict == "connected"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_bubblewrap, id="bubblewrap"),
        pytest.param(_landlock, id="landlock", marks=_OPEN_ISSUE),
        pytest.param(_seatbelt, id="seatbelt", marks=_OPEN_ISSUE),
    ],
)
def test_a_host_confined_agent_cannot_reach_the_control_socket(
    build: Callable[[Path], WorkspaceSandbox],
    control_socket: Path,
    workspace: Path,
) -> None:
    sandbox = build(workspace)
    result = run_test_command(
        sandbox.wrap([_INTERPRETER, "-I", "-c", _PROBE, str(control_socket)]),
        capture_output=True,
        text=True,
        check=False,
        cwd=workspace,
        env=dict(sandbox.env),
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"the probe exited {result.returncode}: {result.stderr}")
    assert not _connects(result.stdout), result.stdout


def test_an_agent_in_a_docker_run_environment_cannot_reach_the_control_socket(
    control_socket: Path, tmp_path: Path
) -> None:
    if os.environ.get(_DOCKER_ENV) != "1" or shutil.which("docker") is None:
        pytest.skip(f"set {_DOCKER_ENV}=1 with docker on PATH to run against a real daemon")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    launcher = SubprocessPipedProcessLauncher()
    request = RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=LocalBackend(
            ComputeBackend.CPU,
            log_dir,
            image=_BASE_IMAGE,
            docker=SubprocessDockerCli(processes=OsProcesses(), launcher=launcher),
            threads=OsThreads(),
            launcher=launcher,
        ),
        agent_backend="stub",
        cli_provider=None,
        run_id="control-socket-probe",
        framework_root=tmp_path,
        project_path_policy=ProjectPathPolicy(),
        clock=MonotonicClock(),
        system=EnvironmentSystem(
            threads=OsThreads(),
            network=UnixNetwork(),
            processes=OsProcesses(),
            launcher=launcher,
            slurm_processes=NO_PROCESSES,
        ),
    )
    session = open_run_environment(build_run_environment(RunEnvironmentSpec("docker")), request)
    try:
        script = _PROBE.replace("'", "'\"'\"'")
        result = session.sandbox.execute(
            f"python3 -I -c '{script}' {control_socket}", output=DISCARD
        )
    finally:
        session.close()

    if result.exit_code != 0:
        pytest.fail(f"the probe exited {result.exit_code}: {result.output}")
    assert not _connects(result.output), result.output
