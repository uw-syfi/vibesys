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
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.launcher import headless_command, source_checkout_root
from server.host_agent_logins import (
    AccountLogin,
    AgentLogin,
    ApiKeyLogin,
    LoginStarted,
    LoginState,
    LoginUnavailable,
    LoginUnavailableReason,
)
from server.host_api import HarnessOption
from server.host_harness import HarnessDisplay, Maker, ModelGeneration
from server.host_helper import serve_channel
from server.host_helper_compute import MachineCompute
from server.host_helper_logs import HelperLogs, run_location
from server.host_helper_machine import HostMachineProbe
from server.host_helper_os import (
    FileProjects,
    GitCheckoutProbe,
    HeldLogin,
    LoginBounds,
    LoginStarter,
    OsConfigFiles,
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
    from collections.abc import Callable, Mapping

    from server.host_examples import CreatedProject, ExampleDestination, ExampleList
    from server.host_helper_requests import ExampleProjects
    from vs_agent.api import LoginCommand, ModelDisplay, ProviderLogin
    from vs_sim.api import Threads

_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2


def _maker(vendor: str) -> Maker | None:
    """The maker agentshim's ``vendor`` names, or ``None`` for a vendor this contract lacks."""
    return next((maker for maker in Maker if maker.value == vendor), None)


def _generation(shown: ModelDisplay | None) -> ModelGeneration | None:
    """The generation agentshim ranks the model in, or ``None`` when it names none."""
    if shown is None or shown.generation is None:
        return None
    return ModelGeneration(
        id=shown.generation.id, name=shown.generation.name, order=shown.generation.rank
    )


@cache
def vetted_harnesses() -> tuple[HarnessOption, ...]:
    """The agent library's vetted harnesses, each provider's default marked; loaded on first use."""
    # lint-waiver: LW-271503 [PLC0415]; the helper answers hello without loading the agent library.
    # > `vs_agent.api` owns the vetted list and takes about half a second to import; at module
    # > level every channel would pay for it before `hello` (the light-entrypoint guards forbid
    # > it). A copy of the list in a light module would be a second source of truth, and a
    # > generated copy would need a drift check for what one deferred import already gives.
    from vs_agent.api import (  # noqa: PLC0415
        default_model,
        harnesses,
        model_display,
        provider_display_name,
    )

    options: list[HarnessOption] = []
    for harness in harnesses():
        shown = model_display(harness.provider, harness.model)
        options.append(
            HarnessOption(
                provider=harness.provider,
                model=harness.model,
                default=default_model(harness.provider) == harness.model,
                display=HarnessDisplay(
                    provider_name=provider_display_name(harness.provider),
                    model_name=None if shown is None else shown.model_name,
                    family=None if shown is None else shown.family,
                    tier=None if shown is None else shown.tier,
                    tier_name=None if shown is None else shown.tier_name,
                    maker=None if shown is None else _maker(shown.vendor),
                    generation=_generation(shown),
                ),
            )
        )
    return tuple(options)


@cache
def harness_binaries() -> tuple[tuple[str, str], ...]:
    """Each vetted provider, in offer order, with its CLI's program name; loaded on first use."""
    # lint-waiver: LW-271504 [PLC0415]; the helper answers hello without loading the agent library.
    # > The same deferral as `vetted_harnesses`: `vs_agent.api` owns the vetted list and the
    # > provider profiles that name each CLI, and importing it at module level would make every
    # > channel pay half a second before `hello`; a copied list would be a second source of truth.
    from vs_agent.api import harnesses, provider_binary  # noqa: PLC0415

    providers = dict.fromkeys(harness.provider for harness in harnesses())
    return tuple((provider, provider_binary(provider)) for provider in providers)


def launch_environment() -> Mapping[str, str]:
    """The environment a run finds provider CLIs in; loaded on first use."""
    # lint-waiver: LW-271506 [PLC0415]; the helper answers hello without loading the agent library.
    # > The same deferral as `harness_binaries`: `vs_agent.api` owns how a run finds a provider
    # > CLI (the interactive login shell's environment), and the helper may itself have been
    # > started by a non-login shell whose `PATH` lacks it; a copy of that lookup here would
    # > drift from the run's.
    from vs_agent.api import launch_shell_environment  # noqa: PLC0415

    return launch_shell_environment()


def provider_login_here(provider: str) -> ProviderLogin:
    """``provider``'s CLI probed in the environment a run finds it in, with this user's home."""
    # lint-waiver: LW-271508 [PLC0415]; the helper answers hello without loading the agent library.
    # > The same deferral as `vetted_harnesses`: `vs_agent.api` owns the provider facts and
    # > importing it at module level would make every channel pay half a second before `hello`.
    from vs_agent.api import provider_login  # noqa: PLC0415

    return provider_login(provider, env=launch_environment(), home=Path.home())


def provider_login_command_here(provider: str) -> LoginCommand | None:
    """The command that signs ``provider``'s CLI in, or ``None`` when its CLI has none."""
    # lint-waiver: LW-271509 [PLC0415]; the helper answers hello without loading the agent library.
    # > The same deferral as `provider_login_here`: `vs_agent.api` owns the provider facts and
    # > importing it at module level would make every channel pay half a second before `hello`.
    from vs_agent.api import provider_login_command  # noqa: PLC0415

    return provider_login_command(provider)


class AgentLibraryLogins:
    """Each vetted harness's login, read through the agent library (agentshim's facts).

    ``login_of`` probes one provider's CLI; the agent library is loaded when it first runs, not
    for ``hello``.
    """

    def __init__(
        self,
        login_of: Callable[[str], ProviderLogin],
        command_of: Callable[[str], LoginCommand | None],
        starter: LoginStarter,
    ) -> None:
        """Bind how a CLI is probed, which command signs it in, and how that command is run.

        ``provider_login_here`` and ``provider_login_command_here`` on a real host.
        """
        self._login_of = login_of
        self._command_of = command_of
        self._starter = starter

    def providers(self) -> tuple[str, ...]:
        """The providers the vetted harnesses come from, in offer order."""
        return tuple(provider for provider, _binary in harness_binaries())

    def list(self) -> tuple[AgentLogin, ...]:
        """Each provider's CLI probed for whether it is installed and signed in, and how."""
        return tuple(self._login(provider) for provider in self.providers())

    def start(self, provider: str) -> LoginStarted | LoginUnavailable:
        """Run the harness's own login command, or say why it cannot be run from here.

        A harness that is not installed is reported as that first. A command that prompts the
        person is not run: it is named, for a terminal.
        """
        if not self._login_of(provider).installed:
            return LoginUnavailable(reason=LoginUnavailableReason.NOT_INSTALLED)
        command = self._command_of(provider)
        if command is None:
            return LoginUnavailable(reason=LoginUnavailableReason.NO_LOGIN_COMMAND)
        if command.needs_terminal:
            return LoginUnavailable(
                reason=LoginUnavailableReason.NEEDS_TERMINAL, command=command.line
            )
        return self._starter.start(command.argv)

    def hold(self, provider: str) -> HeldLogin:
        """Start ``provider``'s login and keep its process group until cancel or completion.

        The same refusals as ``start``. A provider this host does not offer raises ``ValueError``.
        """
        if provider not in self.providers():
            message = f"this host offers no harness for provider {provider!r}"
            raise ValueError(message)
        if not self._login_of(provider).installed:
            return self._starter.refuse(
                LoginUnavailable(reason=LoginUnavailableReason.NOT_INSTALLED)
            )
        command = self._command_of(provider)
        if command is None:
            return self._starter.refuse(
                LoginUnavailable(reason=LoginUnavailableReason.NO_LOGIN_COMMAND)
            )
        if command.needs_terminal:
            return self._starter.refuse(
                LoginUnavailable(reason=LoginUnavailableReason.NEEDS_TERMINAL, command=command.line)
            )
        return self._starter.hold(command.argv)

    def _login(self, provider: str) -> AgentLogin:
        # lint-waiver: LW-271507 [PLC0415]; the helper answers hello without loading the agent library.
        # > The same deferral as `vetted_harnesses`: `vs_agent.api` owns the provider facts and
        # > importing it at module level would make every channel pay half a second before `hello`.
        from vs_agent import api as agents  # noqa: PLC0415

        found = self._login_of(provider)
        command = self._command_of(provider)
        credential = found.credential
        how: AccountLogin | ApiKeyLogin | None = None
        if credential is not None:
            how = (
                ApiKeyLogin(variable=credential.variable, file=credential.file)
                if credential.kind is agents.LoginKind.API_KEY
                else AccountLogin()
            )
        state = {
            agents.LoginState.SIGNED_IN: LoginState.SIGNED_IN,
            agents.LoginState.SIGNED_OUT: LoginState.SIGNED_OUT,
            agents.LoginState.UNKNOWN: LoginState.UNKNOWN,
        }[found.state]
        return AgentLogin(
            provider=provider,
            provider_name=agents.provider_display_name(provider),
            installed=found.installed,
            state=state,
            how=how,
            api_key_variables=found.api_key_variables,
            detail=found.detail,
            can_start_login=found.installed and command is not None and not command.needs_terminal,
        )


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

    def list(self, *, with_catalogue: bool = False) -> ExampleList:
        """Every catalogued example with its tasks, and with their catalogue facts if asked."""
        return checkout_example_projects().list(with_catalogue=with_catalogue)

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        return checkout_example_projects().create(example, destination)

    def destination(self, example: str, destination: Path) -> tuple[ExampleDestination, str | None]:
        """What :meth:`create` would find at ``destination``, and the project there if it is one."""
        return checkout_example_projects().destination(example, destination)

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        return checkout_example_projects().copy_out(source, destination)


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
        examples=ExampleProjectsOnUse(),
        checkout=GitCheckoutProbe(source_checkout_root(), SubprocessProbe()),
        dialer=network,
        vibesys_version=host_facts()[1],
        logs=HelperLogs(logs, threads),
        harnesses=vetted_harnesses,
        machine=HostMachineProbe(
            SubprocessProbe(),
            cpu_cores=os.cpu_count,
            which=lambda program, path: shutil.which(program, path=path),
            harness_binaries=harness_binaries,
            launch_environment=launch_environment,
        ),
        logins=AgentLibraryLogins(
            provider_login_here,
            provider_login_command_here,
            LoginStarter(
                SubprocessPipedProcessLauncher(), threads, launch_environment, LoginBounds()
            ),
        ),
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
