"""The host facts a ``vibesys host <read>`` answers from, each built when the read asks for it.

``vibesys host <read>`` is a new process for every read, so what a read builds is paid by every
read. Each fact is therefore built the first time it is asked for and kept (``cached_property``),
and the modules behind it are lazy modules (``server.lazy_module``): ``list-compute`` never
constructs the project catalog, the registry or the agent library, and ``list-tasks`` never
constructs the compute settings.

The host helper builds the same facts, all of them, when it starts
(``entrypoints.host_helper.real_host``), and wires them itself: it imports them statically before
it answers ``hello``, and this module and its lazy modules would add to what that counts
(``tests/e2e/test_host_helper.py``). ``tests/entrypoints/test_host_facts.py`` holds the two
wirings to each other.

Public surface: ``HostFacts``.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from server import lazy_module
from vs_sim.api import ProcessesProbe, SubprocessPipedProcessLauncher

if TYPE_CHECKING:
    from collections.abc import Mapping

    from entrypoints import host_example_projects, host_harnesses
    from server import host_agent_logins, host_api, host_compute_runtime, host_helper_os
    from server import registry as live_registry
    from server.host_helper_requests import ExampleProjects
    from vs_project import api as project_library
    from vs_sim.api import Processes, SleepingClock, Threads
else:
    host_example_projects = lazy_module("entrypoints.host_example_projects")
    host_harnesses = lazy_module("entrypoints.host_harnesses")
    host_agent_logins = lazy_module("server.host_agent_logins")
    host_compute_runtime = lazy_module("server.host_compute_runtime")
    host_helper_os = lazy_module("server.host_helper_os")
    live_registry = lazy_module("server.registry")
    project_library = lazy_module("vs_project.api")


class HostFacts:
    """The host's collaborators for catalog and project reads, each built on first use."""

    def __init__(
        self,
        environ: Mapping[str, str],
        uid: int,
        *,
        clock: SleepingClock,
        threads: Threads,
        processes: Processes,
    ) -> None:
        """Bind this process's environment, user, epoch clock, threads and processes.

        Nothing is constructed yet. ``clock`` is the epoch clock records age on.
        """
        self._environ = environ
        self._uid = uid
        self.clock = clock
        self._threads = threads
        self._processes = processes

    @cached_property
    def instance_root(self) -> Path:
        """This user's private runtime root; ``PermissionError`` when it is unsafe."""
        return live_registry.instance_root(self._environ, self._uid)

    @cached_property
    def registry(self) -> live_registry.LiveRegistry:
        """The servers this host runs or ran."""
        return live_registry.LiveRegistry(
            live_registry.FileInstanceStore(self.instance_root), self.clock
        )

    @cached_property
    def projects(self) -> host_helper_os.FileProjects:
        """Project metadata, authored tasks and the runs' journals on this filesystem."""
        return host_helper_os.FileProjects(
            project_library.git_repositories(project_library.Git(self._processes)),
            self._threads,
            clock=self.clock,
        )

    @cached_property
    def directories(self) -> host_helper_os.OsDirectories:
        """Path expansion and the home directory."""
        return host_helper_os.OsDirectories()

    @cached_property
    def examples(self) -> ExampleProjects:
        """This checkout's example catalog and project maker; only an example read asks for it."""
        return host_example_projects.checkout_example_projects(self._processes)

    @cached_property
    def compute(self) -> host_compute_runtime.MachineCompute:
        """The compute settings, from this account's configuration files."""
        return host_compute_runtime.MachineCompute(
            host_compute_runtime.OsConfigFiles(),
            ProcessesProbe(self._processes),
            home=str(Path.home()),
            environ=self._environ,
        )

    @cached_property
    def logins(self) -> host_agent_logins.AgentLibraryLogins:
        """Each vetted harness's login facts."""
        return host_agent_logins.host_logins(SubprocessPipedProcessLauncher(), self._threads)

    def harnesses(self) -> tuple[host_api.HarnessOption, ...]:
        """The agent library's vetted harnesses, in the catalog's order."""
        return host_harnesses.vetted_harnesses()
