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
from typing import TYPE_CHECKING

from entrypoints.host_facts import HostFacts
from entrypoints.launcher import headless_command, source_checkout_root
from server.host_agent_logins import harness_binaries, launch_environment
from server.host_helper import serve_channel
from server.host_helper_logs import HelperLogs, run_location
from server.host_helper_machine import HostMachineProbe
from server.host_helper_os import (
    GitCheckoutProbe,
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
)
from server.relay import FdSink, FdSource
from vs_project.api import OperatorDialer
from vs_sim.api import (
    OsProcesses,
    OsThreads,
    PidfdProcessSignaller,
    SubprocessLauncher,
    SubprocessProbe,
    SystemClock,
    UnixNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_sim.api import Threads

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2


def real_host(environ: Mapping[str, str], uid: int, threads: Threads) -> HelperHost:
    """This host as the helper sees it; ``PermissionError`` when the registry root is unsafe.

    The facts a read also answers from (the registry, projects, directories, examples, logins,
    compute settings and harnesses) are ``HostFacts``', so the helper and a read build them alike.
    """
    # Records carry epoch times, so they age on the epoch clock, not ``threads``' monotonic one.
    facts = HostFacts(environ, uid, clock=SystemClock(), threads=threads, processes=OsProcesses())
    epoch = facts.clock
    root = facts.instance_root
    # Every control socket the helper opens (a stop, a relayed ``run.connect``) presents
    # the operator key first: the socket serves nobody else. The helper only reads the key,
    # as each connection opens, since it may start before any server has made one.
    network = OperatorDialer.for_user(UnixNetwork(), environ, uid)
    store = FileInstanceStore(root)
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
        registry=facts.registry,
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
        projects=facts.projects,
        directories=facts.directories,
        launcher=SubprocessRunLauncher(SubprocessLauncher(), headless_command),
        preflight=SubprocessRunPreflight(SubprocessLauncher(), headless_command),
        examples=facts.examples,
        checkout=GitCheckoutProbe(source_checkout_root(), SubprocessProbe()),
        dialer=network,
        vibesys_version=host_facts()[1],
        logs=HelperLogs(logs, threads),
        harnesses=facts.harnesses,
        machine=HostMachineProbe(
            SubprocessProbe(),
            cpu_cores=os.cpu_count,
            which=lambda program, path: shutil.which(program, path=path),
            harness_binaries=harness_binaries,
            launch_environment=launch_environment,
        ),
        logins=facts.logins,
        compute=facts.compute,
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
