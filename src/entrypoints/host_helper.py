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
import sys
from typing import TYPE_CHECKING

from entrypoints.launcher import headless_command, source_checkout_root
from server.host_helper import serve_channel
from server.host_helper_os import (
    FileProjects,
    GitCheckoutProbe,
    OsDirectories,
    SubprocessRunLauncher,
)
from server.host_helper_requests import HelperHost
from server.instances import ControlSocketStopRequester
from server.registry import (
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
from vs_sim.api import (
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
    """This host as the helper sees it; ``PermissionError`` when the registry root is unsafe."""
    network = UnixNetwork()
    root = instance_root(environ, uid)
    store = FileInstanceStore(root)
    # Records carry epoch times, so they age on the epoch clock, not ``threads``' monotonic one.
    epoch = SystemClock()
    return HelperHost(
        registry=LiveRegistry(store, epoch),
        inspector=InstanceInspector(
            store,
            FileInstanceLogs(root),
            FileInstanceMetadataStore(root),
            epoch,
        ),
        stop_effects=StopEffects(
            requester=ControlSocketStopRequester(network),
            signaller=PidfdProcessSignaller(),
            clock=threads,
            pause=threads.sleep,
        ),
        projects=FileProjects(),
        directories=OsDirectories(),
        launcher=SubprocessRunLauncher(SubprocessLauncher(), headless_command),
        checkout=GitCheckoutProbe(source_checkout_root(), SubprocessProbe()),
        dialer=network,
        vibesys_version=host_facts()[1],
    )


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
