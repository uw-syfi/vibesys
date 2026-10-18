"""What a confined agent can reach of its run's operator control, per backend, for real.

A run's server listens on ``control.sock`` in the user's runtime root (the path
``server.registry.instance_socket_path`` derives), and whoever it serves can send any
operator control, including a cancel of another attempt (#3133). The socket serves only a
connection that first presents the operator key (``vs_project.api.OperatorKey``), and the
key lives in the runtime root, which every confinement keeps out of the agent's view
(``vs_sandbox.operator_root``).

This suite serves a real control socket at that path and runs probes under each
confinement backend, built the way a run builds it:

- The key probe does what an agent would: it reads the key file if it can, connects,
  sends the hello it could make and a snapshot query, and prints the reply. Bubblewrap and
  Docker must not even show the socket (``ENOENT`` or ``ECONNREFUSED``). Landlock and
  Seatbelt let it connect (no Landlock right covers connecting to a pathname socket, and
  the Seatbelt profile allows ``network*``), so there the socket must answer
  ``control_key_refused``.
- The swap probe moves the socket aside and binds its own in its place, to read the next
  client's hello. In the runtime root it must fail under every backend. In a directory
  Landlock grants (host ``/tmp``, where the web socket used to live) it succeeds,
  so the operator's dialer must refuse to present the key there at all.
- The egress probe connects to the user's D-Bus and the Docker daemon socket, either of
  which runs any command as the operator outside the confinement. A backend whose type
  claims the operator-control boundary
  (``WorkspaceSandbox.claims_operator_control_boundary``) must keep both out of reach. One
  that does not (Landlock, Seatbelt) is pinned to the known gap: the probe connects. A fix
  that closes it fails that assertion, and the fix then flips the claim. On Linux, under
  ``VIBESYS_REQUIRE_SANDBOX_TESTS=1``, a target the host lacks is a failure, not a skip.
- The network probe connects to an abstract Unix socket and a loopback TCP port the test
  listens on, as a ``dbus-launch`` bus (``unix:abstract=``) or a Docker daemon on
  ``127.0.0.1:2375`` would. The claim does not cover them: bubblewrap shares the host's
  network namespace, so both are reachable under it, pinned as known gaps, as they are
  under Landlock (abstract sockets only until Landlock ABI 6 scopes them) and Seatbelt
  (which has no abstract sockets). A Docker container has its own network namespace, so
  neither is reachable from it.

Landlock grants ``/tmp``, so the suite moves the runtime root under the account's home, as
an operator with ``XDG_RUNTIME_DIR`` at ``/run/user/<uid>`` has it; a root under a Landlock
grant is refused when the sandbox is built (``libs/vs-sandbox/tests/test_operator_root.py``).

Bubblewrap, Landlock and Seatbelt skip on a host without them (CI's ``sandbox_tests``
group sets ``VIBESYS_REQUIRE_SANDBOX_TESTS=1`` and gets a failure instead). Docker is
skipped unless ``VIBESYS_E2E_DOCKER=1``, which no CI group sets, so the Docker test is run
by hand:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_control_socket_reachability_e2e.py -q
```
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import shlex
import shutil
import socket
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest
from tests.server.support import REQUEST_TIME, build_server_parts
from tests.support import run_test_command

from server.api.protocol import ProtocolErrorMessage, SnapshotQuery
from server.registry import (
    instance_root,
    instance_socket_path,
    new_instance_id,
)
from server.transport.unix_jsonl import ControlKeyRefusedError, UnixJsonlServer
from vibesys.run.environment import open_run_environment
from vs_project.api import (
    OperatorDialer,
    OperatorSocketOutsideRootError,
    create_operator_key,
    operator_key_path,
)
from vs_runtime.api.infrastructure import (
    EnvironmentSystem,
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
)
from vs_sandbox import host_sandbox, landlock
from vs_sandbox.api import (
    ComputeBackend,
    DockerSandbox,
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

#: Arguments: the socket, the key file, and the request line. Prints one JSON object:
#: ``key`` (the key file's contents, or an errno name), ``connect`` (``ok`` or an errno
#: name) and ``reply`` (the first line the socket sent back, or ``null``). Output that is
#: not that object means the probe did not run, which must not pass for a refusal.
_PROBE = """\
import errno, json, socket, sys
socket_path, key_path, request = sys.argv[1:4]
seen = {"key": None, "connect": None, "reply": None}
hello = ""
try:
    with open(key_path) as key_file:
        seen["key"] = key_file.read()
    hello = json.dumps({"control_key": seen["key"].strip()}, separators=(",", ":")) + "\\n"
except OSError as error:
    seen["key"] = errno.errorcode.get(error.errno, str(error.errno))
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.settimeout(30)
try:
    connection.connect(socket_path)
    seen["connect"] = "ok"
    connection.sendall((hello + request + "\\n").encode())
    reply = b""
    while not reply.endswith(b"\\n"):
        chunk = connection.recv(65536)
        if not chunk:
            break
        reply += chunk
    seen["reply"] = reply.decode() or None
except OSError as error:
    seen["connect"] = seen["connect"] or errno.errorcode.get(error.errno, str(error.errno))
print(json.dumps(seen))
"""

#: Argument: the socket. Moves it aside and binds a listener in its place, as an agent
#: stealing the next hello would. Prints ``rename`` and ``bind`` (``ok`` or an errno name).
_SWAP_PROBE = """\
import errno, json, os, socket, sys
socket_path = sys.argv[1]
seen = {"rename": None, "bind": None}
def name(error):
    return errno.errorcode.get(error.errno, str(error.errno))
try:
    os.rename(socket_path, socket_path + ".real")
    seen["rename"] = "ok"
except OSError as error:
    seen["rename"] = name(error)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    listener.bind(socket_path)
    listener.listen(1)
    seen["bind"] = "ok"
except OSError as error:
    seen["bind"] = name(error)
print(json.dumps(seen))
"""

#: Arguments: sockets. Prints ``{path: "ok" or an errno name}``, one per connect attempt.
_EGRESS_PROBE = """\
import errno, json, socket, sys
seen = {}
for path in sys.argv[1:]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(30)
    try:
        connection.connect(path)
        seen[path] = "ok"
    except OSError as error:
        seen[path] = errno.errorcode.get(error.errno, str(error.errno))
    finally:
        connection.close()
print(json.dumps(seen))
"""

#: Arguments: ``abstract:<name>`` or ``tcp:<port>`` targets. Prints ``{target: "ok" or an
#: errno name}``, one per connect attempt, to the abstract socket or to ``127.0.0.1``.
_NETWORK_PROBE = """\
import errno, json, socket, sys
seen = {}
for target in sys.argv[1:]:
    kind, _, value = target.partition(":")
    if kind == "abstract":
        connection, address = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), "\\0" + value
    else:
        connection, address = socket.socket(), ("127.0.0.1", int(value))
    connection.settimeout(30)
    try:
        connection.connect(address)
        seen[target] = "ok"
    except OSError as error:
        seen[target] = errno.errorcode.get(error.errno, str(error.errno))
    finally:
        connection.close()
print(json.dumps(seen))
"""

_REQUEST = SnapshotQuery(timestamp=REQUEST_TIME).model_dump_json()

#: Sockets that run any command as the operator outside every confinement. The bus is
#: systemd's per-user one; the test session moves ``XDG_RUNTIME_DIR``, so it is not read.
_EGRESS_TARGETS = {
    "user-bus": Path("/run/user") / str(os.getuid()) / "bus",
    "docker": Path("/var/run/docker.sock"),
}
#: What a probe sees of a socket its confinement keeps out of its view.
_UNREACHABLE = frozenset({"ENOENT", "ECONNREFUSED"})
_KNOWN_GAP = (
    "known gap, pinned on purpose: {backend} does not restrict connect() to pathname Unix "
    "sockets, so the agent reaches {target} and escapes through it "
    "(docs/contributing/sandboxing.md, Known limits). If this assertion fails, the gap is "
    "closed: set {backend}.claims_operator_control_boundary = True and update the docs."
)


_NETWORK_GAP = (
    "known gap, pinned on purpose: {backend} shares the host's network namespace for "
    "{channel}, so the agent reaches a listener there (docs/contributing/sandboxing.md, "
    "Known limits). If this assertion fails, the gap is closed: update the docs and "
    "WorkspaceSandbox.claims_operator_control_boundary's docstring."
)


def _absent(reason: str) -> NoReturn:
    """Skip, or fail where CI requires the real confinement backends."""
    if _REQUIRED:
        pytest.fail(reason)
    pytest.skip(reason)


@dataclass(frozen=True)
class _ControlSocket:
    """A served control socket, the operator key file it demands, and the operator's dialer."""

    path: Path
    key_file: Path
    dialer: OperatorDialer


@pytest.fixture
def probe_root() -> Iterator[Path]:
    """A directory outside ``/tmp``: Landlock refuses a project or runtime root under a grant.

    The test session moves ``HOME`` under ``/tmp``, so the account's own home is read
    from the password database.
    """
    parent = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="vs-sock-") as root:
        yield Path(root)


@pytest.fixture
def control_socket(probe_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_ControlSocket]:
    """A run's control socket, served where a run's server binds it, with the real key."""
    runtime = probe_root / "run"
    runtime.mkdir(mode=0o700)
    # The operator's environment: the server, its clients and every confinement built
    # below read the runtime root from it.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = instance_root(os.environ, os.getuid())
    key = create_operator_key(os.environ, os.getuid())
    path = instance_socket_path(root, new_instance_id())
    threads = OsThreads()
    parts = build_server_parts(threads=threads)
    network = UnixNetwork()
    # The operator's own client, as `vibesys instances` and the host helper build it.
    dialer = OperatorDialer.for_user(network, os.environ, os.getuid())
    try:
        with UnixJsonlServer(path, parts.api, None, threads, network, key=key):
            served = _ControlSocket(path, operator_key_path(os.environ, os.getuid()), dialer)
            assert _served(_reply_to_operator(served))
            yield served
    finally:
        parts.close()


@pytest.fixture
def workspace(probe_root: Path) -> Path:
    """The confined project, beside the runtime root."""
    project = probe_root / "project"
    project.mkdir()
    return project


def _reply_to_operator(served: _ControlSocket) -> str:
    """What the socket answers the operator's own client: the control that must keep working."""
    connection = served.dialer.connect(str(served.path), HANG_GUARD_S)
    try:
        connection.send(_REQUEST.encode() + b"\n", HANG_GUARD_S)
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = connection.recv(65536, HANG_GUARD_S)
            if not chunk:
                break
            reply += chunk
    finally:
        connection.close()
    return reply.decode()


def _served(reply: str | None) -> bool:
    """Whether ``reply`` is the socket serving the request, not refusing the connection."""
    if not reply:
        return False
    message = json.loads(reply)
    diagnostic = message.get("diagnostic") or {}
    return diagnostic.get("code") != ControlKeyRefusedError.diagnostic_code


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


def _probe_saw(output: str, fields: set[str]) -> dict[str, str | None]:
    """What the probe saw; fails the test if the probe did not run."""
    lines = output.strip().splitlines()
    try:
        seen = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        seen = None
    if not isinstance(seen, dict) or set(seen) != fields:
        pytest.fail(f"the probe did not run: {output!r}")
    return seen


def _assert_refused(
    seen: dict[str, str | None], served: _ControlSocket, sandbox: WorkspaceSandbox
) -> None:
    """The probe did not read the key, and could not reach or was refused by the socket.

    A confinement that claims the operator-control boundary must not show the socket at
    all. Where the socket is visible, only a connected probe that read the server's
    ``control_key_refused`` passes: a reset, a timeout or an empty reply proves nothing.
    """
    key = served.key_file.read_text()
    assert seen["key"] != key, "the confined probe read the operator key"
    if sandbox.claims_operator_control_boundary:
        assert seen["connect"] in _UNREACHABLE, f"the confined probe reached the socket: {seen}"
        return
    assert seen["connect"] == "ok", f"the probe could not connect, so nothing was tested: {seen}"
    reply = seen["reply"]
    assert reply, f"the socket sent the probe nothing: {seen}"
    refusal = ProtocolErrorMessage.model_validate_json(reply)
    assert refusal.diagnostic is not None, refusal
    assert refusal.diagnostic.code == ControlKeyRefusedError.diagnostic_code, refusal


def _run_confined(sandbox: WorkspaceSandbox, workspace: Path, probe: str, *arguments: str) -> str:
    """Run ``probe`` under ``sandbox`` with the system interpreter; return its stdout."""
    result = run_test_command(
        sandbox.wrap([_INTERPRETER, "-I", "-c", probe, *arguments]),
        capture_output=True,
        text=True,
        check=False,
        cwd=workspace,
        env=dict(sandbox.env),
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"the probe exited {result.returncode}: {result.stderr}")
    return result.stdout


_HOST_BACKENDS = [
    pytest.param(_bubblewrap, id="bubblewrap"),
    pytest.param(_landlock, id="landlock"),
    pytest.param(_seatbelt, id="seatbelt"),
]


@pytest.mark.parametrize("build", _HOST_BACKENDS)
def test_a_host_confined_agent_is_refused_by_the_control_socket(
    build: Callable[[Path], WorkspaceSandbox],
    control_socket: _ControlSocket,
    workspace: Path,
) -> None:
    sandbox = build(workspace)
    output = _run_confined(
        sandbox,
        workspace,
        _PROBE,
        str(control_socket.path),
        str(control_socket.key_file),
        _REQUEST,
    )

    _assert_refused(_probe_saw(output, {"key", "connect", "reply"}), control_socket, sandbox)


@pytest.mark.parametrize("build", _HOST_BACKENDS)
def test_a_host_confined_agent_cannot_swap_the_control_socket(
    build: Callable[[Path], WorkspaceSandbox],
    control_socket: _ControlSocket,
    workspace: Path,
) -> None:
    """The runtime root is not the agent's to write, so the operator's next hello is safe."""
    sandbox = build(workspace)
    output = _run_confined(sandbox, workspace, _SWAP_PROBE, str(control_socket.path))

    seen = _probe_saw(output, {"rename", "bind"})
    assert seen["rename"] != "ok", f"the confined probe moved the control socket: {seen}"
    assert seen["bind"] != "ok", f"the confined probe bound the control socket's path: {seen}"
    assert _served(_reply_to_operator(control_socket))


def test_the_operator_presents_no_key_to_a_socket_landlock_lets_the_agent_swap(
    control_socket: _ControlSocket, workspace: Path
) -> None:
    """In a directory Landlock grants, the swap works; the operator's dialer refuses it.

    This is where the web socket used to live (a temporary directory in host
    ``/tmp``). The agent replaces the socket with its own listener, and the operator's
    client, which would have sent its hello to that listener, refuses before connecting.
    """
    sandbox = _landlock(workspace)
    assert isinstance(sandbox, LandlockSandbox)
    granted = next(
        (Path(root) for root in sandbox.scratch_write_roots if Path(root).name == "tmp"), None
    )
    if granted is None or not granted.is_dir():
        pytest.skip("this Landlock ruleset grants no host tmp directory")
    threads = OsThreads()
    parts = build_server_parts(threads=threads)
    network = UnixNetwork()
    key = create_operator_key(os.environ, os.getuid())
    try:
        with tempfile.TemporaryDirectory(dir=granted, prefix="vibesys-session-") as session:
            legacy = Path(session) / "control.sock"
            with UnixJsonlServer(legacy, parts.api, None, threads, network, key=key):
                output = _run_confined(sandbox, workspace, _SWAP_PROBE, str(legacy))
                seen = _probe_saw(output, {"rename", "bind"})
                # The attack works here: the agent now owns what answers at this path.
                assert seen == {"rename": "ok", "bind": "ok"}, seen
                assert stat.S_ISSOCK(legacy.lstat().st_mode)
                with pytest.raises(OperatorSocketOutsideRootError):
                    control_socket.dialer.connect(str(legacy), HANG_GUARD_S)
    finally:
        parts.close()


@pytest.mark.parametrize("build", _HOST_BACKENDS)
@pytest.mark.parametrize("target", sorted(_EGRESS_TARGETS))
def test_a_host_confined_agent_reaches_the_user_bus_and_docker_only_where_the_gap_is_known(
    build: Callable[[Path], WorkspaceSandbox],
    target: str,
    control_socket: _ControlSocket,
    workspace: Path,
) -> None:
    """Only a confinement that does not claim the operator-control boundary lets them through.

    Either socket gives a full escape: ``systemd-run --user --pipe cat <key>`` over the
    bus, or a container with the host's filesystem over Docker.
    """
    del control_socket  # Only for its runtime root, outside every Landlock grant.
    path = _EGRESS_TARGETS[target]
    _require_reachable_on_host(path)
    sandbox = build(workspace)
    seen = json.loads(_run_confined(sandbox, workspace, _EGRESS_PROBE, str(path)))[str(path)]

    if sandbox.claims_operator_control_boundary:
        assert seen in _UNREACHABLE, f"{type(sandbox).__name__} let the agent reach {path}: {seen}"
    else:
        assert seen == "ok", _KNOWN_GAP.format(backend=type(sandbox).__name__, target=path)


def _require_reachable_on_host(path: Path) -> None:
    """Skip unless the operator, unconfined, can connect to ``path``: else nothing is probed.

    On Linux under ``VIBESYS_REQUIRE_SANDBOX_TESTS=1`` a missing target fails instead, so a
    CI host without the bus or the daemon cannot pass by probing nothing. Elsewhere it
    skips: both targets are Linux paths, and macOS CI has no Docker daemon.
    """
    missing = _absent if sys.platform.startswith("linux") else pytest.skip
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            missing(f"{path} is not a socket on this host")
    except OSError:
        missing(f"{path} does not exist on this host")
    seen = json.loads(
        run_test_command(
            [sys.executable, "-I", "-c", _EGRESS_PROBE, str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    )[str(path)]
    if seen != "ok":
        missing(f"the operator cannot connect to {path} either ({seen})")


@dataclass(frozen=True)
class _NetworkListeners:
    """A loopback TCP port and, on Linux, an abstract Unix socket, listening on the host."""

    abstract: str | None
    tcp: str

    def targets(self) -> list[str]:
        return [target for target in (self.abstract, self.tcp) if target is not None]


@pytest.fixture
def network_listeners() -> Iterator[_NetworkListeners]:
    """Listeners an agent should not reach, served in the test's own network namespace.

    Abstract Unix sockets exist only on Linux; elsewhere only the TCP listener is made.
    """
    with ExitStack() as stack:
        tcp = stack.enter_context(socket.socket())
        # Nothing accepts: a probe's connect completes on the backlog alone.
        tcp.settimeout(HANG_GUARD_S)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        abstract = None
        if sys.platform.startswith("linux"):
            listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            listener.settimeout(HANG_GUARD_S)
            name = f"vibesys-probe-{secrets.token_hex(8)}"
            listener.bind("\0" + name)
            listener.listen(1)
            abstract = f"abstract:{name}"
        yield _NetworkListeners(abstract, f"tcp:{tcp.getsockname()[1]}")


def _network_gap_expected(sandbox: WorkspaceSandbox, channel: str) -> bool:
    """Whether ``sandbox`` is known to let the agent reach ``channel`` on the host."""
    if channel == "abstract" and isinstance(sandbox, LandlockSandbox):
        return not landlock.supports_scoping()
    # Bubblewrap applies no Landlock scope, so its abstract gap stays open under ABI 6.
    return True


@pytest.mark.parametrize("build", _HOST_BACKENDS)
@pytest.mark.parametrize("channel", ["abstract", "tcp"])
def test_a_host_confined_agent_reaches_host_abstract_sockets_and_loopback_tcp_as_pinned(
    build: Callable[[Path], WorkspaceSandbox],
    channel: str,
    network_listeners: _NetworkListeners,
    control_socket: _ControlSocket,
    workspace: Path,
) -> None:
    """No host backend has a network namespace of its own: a known gap, pinned.

    A session bus started by ``dbus-launch`` listens on an abstract socket, and a Docker
    daemon may listen on ``127.0.0.1:2375``; either runs any command as the operator.
    Landlock ABI 6 scopes abstract sockets, which flips that pin for Landlock.
    """
    del control_socket  # Only for its runtime root, outside every Landlock grant.
    target = network_listeners.abstract if channel == "abstract" else network_listeners.tcp
    if target is None:
        pytest.skip("abstract Unix sockets are Linux only")
    sandbox = build(workspace)
    seen = _probe_saw(_run_confined(sandbox, workspace, _NETWORK_PROBE, target), {target})

    if _network_gap_expected(sandbox, channel):
        assert seen[target] == "ok", _NETWORK_GAP.format(
            backend=type(sandbox).__name__, channel=channel
        )
    else:
        assert seen[target] != "ok", f"{type(sandbox).__name__} reached {target}: {seen}"


def test_an_agent_in_a_docker_run_environment_is_refused_by_the_control_socket(
    control_socket: _ControlSocket, network_listeners: _NetworkListeners, tmp_path: Path
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
        sandbox = session.sandbox
        key_probe = sandbox.execute(
            shlex.join(
                [
                    "python3",
                    "-I",
                    "-c",
                    _PROBE,
                    str(control_socket.path),
                    str(control_socket.key_file),
                    _REQUEST,
                ]
            ),
            output=DISCARD,
        )
        targets = [str(path) for path in _EGRESS_TARGETS.values()]
        egress_probe = sandbox.execute(
            shlex.join(["python3", "-I", "-c", _EGRESS_PROBE, *targets]), output=DISCARD
        )
        network_probe = sandbox.execute(
            shlex.join(["python3", "-I", "-c", _NETWORK_PROBE, *network_listeners.targets()]),
            output=DISCARD,
        )
    finally:
        session.close()

    for result in (key_probe, egress_probe, network_probe):
        if result.exit_code != 0:
            pytest.fail(f"the probe exited {result.exit_code}: {result.output}")
    # The run environment's agent confinement is its started container.
    assert isinstance(sandbox, DockerSandbox)
    assert sandbox.claims_operator_control_boundary
    _assert_refused(
        _probe_saw(key_probe.output, {"key", "connect", "reply"}), control_socket, sandbox
    )
    egress = _probe_saw(egress_probe.output, set(targets))
    assert set(egress.values()) <= _UNREACHABLE, f"the container reached a host socket: {egress}"
    # Its own network namespace: the host's abstract sockets and loopback are not its.
    network = _probe_saw(network_probe.output, set(network_listeners.targets()))
    assert "ok" not in network.values(), f"the container reached a host listener: {network}"
