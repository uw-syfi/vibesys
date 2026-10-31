"""``vibesys host <read>``: answer one registered read with only the host facts it touches.

Every read is a new process, so what a read imports is paid by every read: a catalog read does
tens of milliseconds of work and the import of what it never uses would cost ten times that.
:class:`ReadHost` therefore builds each fact the first time a read asks for it. ``list-compute``
never constructs the project catalog, the registry or the agent library, and ``list-tasks`` never
constructs the compute settings. ``describe-machine`` and ``preflight-run`` genuinely read the
whole host (its registry, machine probes and launcher), so they build the helper's host on use.

Public surface: ``ReadHost`` and ``serve_read``.
"""

from __future__ import annotations

import os
from functools import cached_property
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sim.api import ProcessesProbe, SubprocessPipedProcessLauncher

if TYPE_CHECKING:
    from collections.abc import Mapping

    from server.host_agent_logins import AgentLibraryLogins
    from server.host_api import HarnessOption, HelperError, RunPreflightReply, RunPreflightRequest
    from server.host_compute_runtime import MachineCompute
    from server.host_helper_os import FileProjects, OsDirectories
    from server.host_helper_requests import ExampleProjects, HelperHost
    from server.host_machine_facts import MachineDescription
    from server.registry import LiveRegistry
    from vs_sim.api import Clock, Platform


class ReadHost:
    """The host facts a registered read asks for; each is built on first use and kept."""

    def __init__(self, environ: Mapping[str, str], uid: int, platform: Platform) -> None:
        """Bind this process's environment and platform; nothing is constructed yet."""
        self._environ = environ
        self._uid = uid
        self._platform = platform

    @cached_property
    def clock(self) -> Clock:
        """Wall-clock epoch seconds."""
        return self._platform.clock

    @cached_property
    def projects(self) -> FileProjects:
        """Project metadata, authored tasks and the runs' journals on this filesystem."""
        os_projects = import_module("server.host_helper_os")
        projects = import_module("vs_project.api")
        return os_projects.FileProjects(
            projects.git_repositories(projects.Git(self._platform.processes)),
            self._platform.threads,
            clock=self.clock,
        )

    @cached_property
    def directories(self) -> OsDirectories:
        """Path expansion and the home directory."""
        return import_module("server.host_helper_os").OsDirectories()

    @cached_property
    def examples(self) -> ExampleProjects:
        """This checkout's example catalog and project maker."""
        catalog = import_module("entrypoints.host_example_projects")
        return catalog.checkout_example_projects(self._platform.processes)

    @cached_property
    def compute(self) -> MachineCompute:
        """The compute settings, from this account's configuration files."""
        adapters = import_module("server.host_compute_runtime")
        return adapters.MachineCompute(
            adapters.OsConfigFiles(),
            ProcessesProbe(self._platform.processes),
            home=str(Path.home()),
            environ=self._environ,
        )

    @cached_property
    def registry(self) -> LiveRegistry:
        """The servers this host runs or ran; ``PermissionError`` when its root is unsafe."""
        registry = import_module("server.registry")
        root = registry.instance_root(self._environ, self._uid)
        return registry.LiveRegistry(registry.FileInstanceStore(root), self.clock)

    @cached_property
    def logins(self) -> AgentLibraryLogins:
        """Each vetted harness's login facts."""
        launcher = SubprocessPipedProcessLauncher()
        return import_module("server.host_agent_logins").host_logins(
            launcher, self._platform.threads
        )

    def harnesses(self) -> tuple[HarnessOption, ...]:
        """The agent library's vetted harnesses."""
        return import_module("entrypoints.host_harnesses").vetted_harnesses()

    def describe(self) -> MachineDescription:
        """What this machine offers runs; reads the whole host."""
        requests = import_module("server.host_helper_requests")
        return requests.describe_machine(self._whole_host)

    def preflight(self, request: RunPreflightRequest) -> RunPreflightReply | HelperError:
        """Whether the run these inputs describe can start; reads the whole host."""
        requests = import_module("server.host_helper_requests")
        return requests.preflight_facts(request, self._whole_host)

    @cached_property
    def _whole_host(self) -> HelperHost:
        """The helper's host with every collaborator; built only by a read that needs them all."""
        return import_module("entrypoints.host_helper").real_host(
            self._environ, self._uid, self._platform.threads
        )


def serve_read(cli: str, payload: bytes, *, platform: Platform) -> bytes:
    """Answer registered read ``cli`` for this process's user; the reply is the envelope bytes."""
    host = ReadHost(os.environ, os.getuid(), platform)
    reads = import_module("server.host_operation_reads")
    source = reads.HostReadSource(host, None, describe=host.describe, preflight=host.preflight)
    return reads.answer_registered_read(cli, payload, source)
