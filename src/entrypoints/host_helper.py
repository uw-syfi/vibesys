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
from functools import cache
from typing import TYPE_CHECKING

from entrypoints.launcher import headless_command, source_checkout_root
from server.host_api import HarnessOption
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
from vs_project.api import Git
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
    from pathlib import Path

    from server.host_examples import CreatedProject, ExampleList
    from server.host_helper_requests import ExampleProjects
    from vs_sim.api import Threads

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2


@cache
def vetted_harnesses() -> tuple[HarnessOption, ...]:
    """The agent library's vetted harnesses, each provider's default marked; loaded on first use."""
    # lint-waiver: LW-271503 [PLC0415]; the helper answers hello without loading the agent library.
    # > `vs_agent.api` owns the vetted list and takes about half a second to import; at module
    # > level every channel would pay for it before `hello` (the light-entrypoint guards forbid
    # > it). A copy of the list in a light module would be a second source of truth, and a
    # > generated copy would need a drift check for what one deferred import already gives.
    from vs_agent.api import HARNESSES, default_model  # noqa: PLC0415

    return tuple(
        HarnessOption(
            provider=harness.provider,
            model=harness.model,
            default=default_model(harness.provider) == harness.model,
        )
        for harness in HARNESSES
    )


@cache
def harness_binaries() -> tuple[tuple[str, str], ...]:
    """Each vetted provider, in offer order, with its CLI's program name; loaded on first use."""
    # lint-waiver: LW-271504 [PLC0415]; the helper answers hello without loading the agent library.
    # > The same deferral as `vetted_harnesses`: `vs_agent.api` owns the vetted list and the
    # > provider profiles that name each CLI, and importing it at module level would make every
    # > channel pay half a second before `hello`; a copied list would be a second source of truth.
    from vs_agent.api import HARNESSES, provider_binary  # noqa: PLC0415

    providers = dict.fromkeys(harness.provider for harness in HARNESSES)
    return tuple((provider, provider_binary(provider)) for provider in providers)


@cache
def checkout_example_projects() -> ExampleProjects:
    """This checkout's example catalog and project maker; loaded on first use."""
    # lint-waiver: LW-271505 [PLC0415]; the helper answers hello without loading the example machinery.
    # > `entrypoints.examples` brings argparse, configparser and the catalog reader; at module
    # > level every channel would load them before `hello`, past the helper's module ceiling
    # > (`tests/e2e/test_host_helper.py`). Moving the machinery into the helper would split the
    # > one implementation `vibesys example` uses, and the same deferral serves `harnesses`.
    from entrypoints.examples import GitExampleMaterializer, HelperExampleProjects  # noqa: PLC0415

    return HelperExampleProjects(source_checkout_root(), GitExampleMaterializer(Git(OsProcesses())))


class ExampleProjectsOnUse:
    """The helper's ``ExampleProjects``, which loads the example machinery on its first request."""

    def list(self) -> ExampleList:
        """Every catalogued example with its tasks."""
        return checkout_example_projects().list()

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        return checkout_example_projects().create(example, destination)

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        return checkout_example_projects().copy_out(source, destination)


def real_host(environ: Mapping[str, str], uid: int, threads: Threads) -> HelperHost:
    """This host as the helper sees it; ``PermissionError`` when the registry root is unsafe."""
    network = UnixNetwork()
    root = instance_root(environ, uid)
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
        inspector=inspector,
        stop_effects=StopEffects(
            requester=ControlSocketStopRequester(network, clock=epoch, deadline_clock=threads),
            signaller=PidfdProcessSignaller(),
            clock=threads,
            pause=threads.sleep,
            intents=ProjectStopIntents(),
        ),
        projects=FileProjects(),
        directories=OsDirectories(),
        launcher=SubprocessRunLauncher(SubprocessLauncher(), headless_command),
        preflight=SubprocessRunPreflight(SubprocessLauncher(), headless_command),
        examples=ExampleProjectsOnUse(),
        checkout=GitCheckoutProbe(source_checkout_root(), SubprocessProbe()),
        dialer=network,
        vibesys_version=host_facts()[1],
        logs=HelperLogs(logs, threads),
        harnesses=vetted_harnesses,
        machine=HostMachineProbe(
            SubprocessProbe(),
            cpu_cores=os.cpu_count,
            which=shutil.which,
            harness_binaries=harness_binaries,
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
