"""``python -m entrypoints.host_helper`` (``vibesys-host-helper``): serve one desktop channel.

The desktop app runs this once per channel, locally as a child over stdio and remotely
as the command of one ssh exec channel. It reads typed requests on stdin and writes
replies on stdout until stdin ends, or, after ``run.connect``, relays stdin and stdout
to the run's control socket. ``server.host_helper`` owns the protocol;
``docs/contributing/desktop-connection.md`` ("The host helper") the design.

This module wires the real host: this user's live registry, projects and directories
on this filesystem, launches with the command line ``vibesys --detach`` runs, and the
checkout this module belongs to. Its imports stay within the light allowlist
``tests/architecture/test_light_entrypoints.py`` enforces.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime, tzinfo
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.launcher import headless_command, source_checkout_root
from server.host_agent_logins import (
    harness_binaries,
    host_logins,
    launch_environment,
)
from server.host_compute_runtime import MachineCompute, OsConfigFiles
from server.host_helper import serve_channel
from server.host_helper_logs import HelperLogs, run_location
from server.host_helper_machine import HostMachineProbe
from server.host_helper_os import (
    FileProjects,
    GitCheckoutProbe,
    OsDirectories,
    ProjectStopIntents,
    StoreLogReader,
    SubprocessRunLauncher,
    SubprocessRunPreflight,
    run_streams_directory,
)
from server.host_helper_readers import Readers
from server.host_helper_requests import HelperHost
from server.instances import ControlSocketStopRequester
from server.registry import (
    READERS_DIRECTORY,
    FileInstanceLogs,
    FileInstanceMetadataStore,
    FileInstanceStore,
    InstanceInspector,
    LiveRegistry,
    StopEffects,
    host_facts,
    instance_root,
)
from server.relay import FdSink, FdSource
from vs_project.api import Git, OperatorDialer, git_repositories
from vs_sim.api import (
    OsProcesses,
    OsThreads,
    PidfdProcessSignaller,
    SubprocessLauncher,
    SubprocessPipedProcessLauncher,
    SubprocessProbe,
    SystemClock,
    UnixNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from server.host_api import HarnessOption
    from server.host_examples import CreatedProject, ExampleDestination, ExampleList
    from server.host_helper_requests import ExampleProjects
    from vs_sim.api import Processes, Threads

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2


class ExampleProjectsOnUse:
    """The helper's ``ExampleProjects``, which loads the example machinery on its first request."""

    def __init__(self, processes: Processes) -> None:
        """Bind the processes the example machinery will run Git with."""
        self._processes = processes
        self._projects: ExampleProjects | None = None

    def _loaded(self) -> ExampleProjects:
        if self._projects is None:
            catalog = import_module("entrypoints.host_example_projects")
            self._projects = catalog.checkout_example_projects(self._processes)
        return self._projects

    def list(self, *, with_catalogue: bool = False) -> ExampleList:
        """Every catalogued example with its tasks, and with their catalogue facts if asked."""
        return self._loaded().list(with_catalogue=with_catalogue)

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        return self._loaded().create(example, destination)

    def destination(self, example: str, destination: Path) -> tuple[ExampleDestination, str | None]:
        """What :meth:`create` would find at ``destination``, and the project there if it is one."""
        return self._loaded().destination(example, destination)

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        return self._loaded().copy_out(source, destination)


def _vetted_harnesses() -> tuple[HarnessOption, ...]:
    """The agent library's vetted harnesses; the helper answers ``hello`` without loading them."""
    return import_module("entrypoints.host_harnesses").vetted_harnesses()


def real_host(environ: Mapping[str, str], uid: int, threads: Threads) -> HelperHost:
    """This host as the helper sees it; ``PermissionError`` when the registry root is unsafe."""
    root = instance_root(environ, uid)
    # Every control socket the helper opens (a stop, a relayed ``run.connect``) presents
    # the operator key first: the socket serves nobody else. The helper only reads the key,
    # as each connection opens, since it may start before any server has made one.
    network = OperatorDialer.for_user(UnixNetwork(), environ, uid)
    store = FileInstanceStore(root)
    # Records carry epoch times, so they age on the epoch clock, not ``threads``' monotonic one.
    epoch = SystemClock()
    inspector = InstanceInspector(
        store,
        FileInstanceLogs(root),
        FileInstanceMetadataStore(root),
        epoch,
    )
    logs = StoreLogReader(
        lambda instance_id: run_location(inspector.inspect(instance_id)), run_streams_directory
    )
    return HelperHost(
        registry=LiveRegistry(store, epoch),
        readers=Readers(
            LiveRegistry(FileInstanceStore(root, directory=READERS_DIRECTORY), epoch), threads
        ),
        inspector=inspector,
        stop_effects=StopEffects(
            requester=ControlSocketStopRequester(network, clock=epoch, deadline_clock=threads),
            signaller=PidfdProcessSignaller(),
            clock=threads,
            pause=threads.sleep,
            intents=ProjectStopIntents(),
        ),
        projects=FileProjects(git_repositories(Git(OsProcesses())), threads, clock=epoch),
        directories=OsDirectories(),
        launcher=SubprocessRunLauncher(SubprocessLauncher(), headless_command),
        preflight=SubprocessRunPreflight(SubprocessLauncher(), headless_command),
        examples=ExampleProjectsOnUse(OsProcesses()),
        checkout=GitCheckoutProbe(source_checkout_root(), SubprocessProbe()),
        dialer=network,
        vibesys_version=host_facts()[1],
        logs=HelperLogs(logs, threads),
        harnesses=_vetted_harnesses,
        machine=HostMachineProbe(
            SubprocessProbe(),
            cpu_cores=os.cpu_count,
            which=lambda program, path: shutil.which(program, path=path),
            harness_binaries=harness_binaries,
            launch_environment=launch_environment,
        ),
        logins=host_logins(SubprocessPipedProcessLauncher(), threads),
        compute=MachineCompute(
            OsConfigFiles(), SubprocessProbe(), home=str(Path.home()), environ=environ
        ),
        clock=epoch,
        timezone=local_timezone(epoch.now()),
    )


def local_timezone(now: float) -> tzinfo:
    """This host's local UTC offset at ``now``, which ``machine.describe``'s day follows."""
    timezone = datetime.fromtimestamp(now, UTC).astimezone().tzinfo
    return UTC if timezone is None else timezone


def main() -> None:
    """Serve this process's stdin and stdout as one channel, then exit with its status."""
    threads = OsThreads()
    try:
        host = real_host(os.environ, os.getuid(), threads)
    except PermissionError as exc:
        sys.stderr.write(f"vibesys-host-helper: {exc}\n")
        raise SystemExit(1) from exc
    status = serve_channel(
        host=host,
        source=FdSource(_STDIN_FD),
        output=FdSink(_STDOUT_FD),
        errors=FdSink(_STDERR_FD),
        threads=threads,
    )
    # A non-start request worker, a watch, or the input pump may still be blocked on the
    # registry or stdin. Starts and their cleanup settle before serve_channel returns.
    # The channel's outcome is decided, so leave at once.
    os._exit(status)


if __name__ == "__main__":
    main()
