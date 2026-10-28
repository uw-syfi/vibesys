"""Provider login facts and bounded process ownership for native commands and the helper.

Agentshim through ``vs_agent.api`` owns provider facts. Deferred lookups keep host startup
light; injected process launchers and threads own login output, cancellation and reaping.
Native operations project these facts into generated protobuf messages. The retained helper
request and reply models remain until contraction.

Public surface: the fact and helper message models in ``__all__``, ``AgentLibraryLogins``,
``host_logins``, ``LoginStarter``, ``HeldLogin``, ``LoginBounds``, ``login_instruction``,
``harness_binaries`` and ``launch_environment``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from server.host_envelope import HelperReplyBase, HelperRequestBase
from server.host_machine_facts import LoginState
from vs_sim.api import ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import LoginCommand, ProviderLogin
    from vs_sim.api import PipedProcess, PipedProcessLauncher, Threads

_Line = Annotated[str, StringConstraints(max_length=300, pattern=r"^[^\n\r]*$")]
_Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class AccountLogin(BaseModel):
    """Signed in with a person's account (a subscription login)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["account"] = "account"


class ApiKeyLogin(BaseModel):
    """Signed in with an API key; where it is held is named, the key never."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["api_key"] = "api_key"
    variable: _Name | None = None
    """The environment variable that holds the key."""
    file: _Name | None = None
    """The home-relative file that holds the key."""

    @model_validator(mode="after")
    def _one_place(self) -> ApiKeyLogin:
        if self.variable is not None and self.file is not None:
            message = "an API key is held in a variable or a file, not both"
            raise ValueError(message)
        return self


LoginHow = Annotated[AccountLogin | ApiKeyLogin, Field(discriminator="kind")]
"""How a harness is signed in; ``kind`` discriminates."""


class AgentLogin(BaseModel):
    """One harness's login on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: _Name
    """The harness, as ``harnesses.list`` names its provider."""
    provider_name: _Name
    """The harness as people call it ("Claude Code")."""
    installed: bool
    state: LoginState
    how: LoginHow | None = None
    """How it is signed in; present only when signed in, and absent when the CLI did not say."""
    api_key_variables: tuple[_Name, ...] = ()
    """The environment variables that would sign it in with an API key, in its precedence order."""
    detail: _Line = ""
    """The CLI's own words about its login, one line."""
    can_start_login: bool
    """Whether ``agent_logins.start`` can start this harness's own login on the machine."""

    @model_validator(mode="after")
    def _facts_agree(self) -> AgentLogin:
        if not self.installed and self.state is not LoginState.UNKNOWN:
            message = "a harness that is not installed has an unknown login"
            raise ValueError(message)
        if self.how is not None and self.state is not LoginState.SIGNED_IN:
            message = "how is named only for a signed-in harness"
            raise ValueError(message)
        return self


class LoginStarted(BaseModel):
    """The harness's own login was started on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["started"] = "started"
    instruction: _Line | None = None
    """What the login asks the person to do (a URL to open, a code), when it said."""


class LoginUnavailableReason(StrEnum):
    """Why a login could not be started."""

    NOT_INSTALLED = "not_installed"
    NO_LOGIN_COMMAND = "no_login_command"
    """agentshim names no command that starts this harness's login."""
    NEEDS_TERMINAL = "needs_terminal"
    """The login prompts the person, so only a terminal can finish it: ``command`` is what to run."""
    START_FAILED = "start_failed"
    """The login's command could not run, or ended without asking the person anything."""


class LoginUnavailable(BaseModel):
    """The login was not started, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["unavailable"] = "unavailable"
    reason: LoginUnavailableReason
    command: _Line | None = None
    """The command that signs the harness in, to run in a terminal; sent when the harness has one."""
    detail: _Line = ""
    """What the command said when it could not start, one line."""


LoginStartResult = Annotated[LoginStarted | LoginUnavailable, Field(discriminator="outcome")]
"""What asking for a login did; ``outcome`` discriminates."""


class AgentLoginsListRequest(HelperRequestBase):
    """Each vetted harness's login on this machine."""

    type: Literal["agent_logins.list"] = "agent_logins.list"


class AgentLoginsStartRequest(HelperRequestBase):
    """Start the harness's own login on this machine."""

    type: Literal["agent_logins.start"] = "agent_logins.start"
    provider: _Name
    """A provider ``agent_logins.list`` listed; another is ``unknown_provider``."""


class AgentLoginsListReply(HelperReplyBase):
    """The logins, in the order ``harnesses.list`` offers the providers."""

    type: Literal["agent_logins.list"] = "agent_logins.list"
    logins: tuple[AgentLogin, ...]


class AgentLoginsStartReply(HelperReplyBase):
    """What starting the login did."""

    type: Literal["agent_logins.start"] = "agent_logins.start"
    result: LoginStartResult


@cache
def harness_binaries() -> tuple[tuple[str, str], ...]:
    """Each vetted provider, in offer order, with its CLI's program name; loaded on first use."""
    # lint-waiver: LW-271504 [PLC0415]; the helper answers hello without loading the agent library.
    # > Provider profiles remain authoritative: `vs_agent.api` owns the vetted list and the
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
    # > Provider profiles remain authoritative: `vs_agent.api` owns the provider facts and
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
        threads: Threads,
    ) -> None:
        """Bind how a CLI is probed, which command signs it in, and how that command is run.

        ``provider_login_here`` and ``provider_login_command_here`` on a real host; ``threads``
        probe several CLIs at once and give ``states`` its clock.
        """
        self._login_of = login_of
        self._command_of = command_of
        self._starter = starter
        self._threads = threads

    def providers(self) -> tuple[str, ...]:
        """The providers the vetted harnesses come from, in offer order."""
        return tuple(provider for provider, _binary in harness_binaries())

    def list(self) -> tuple[AgentLogin, ...]:
        """Each provider's CLI probed for whether it is installed and signed in, and how."""
        return tuple(self._login(provider) for provider in self.providers())

    def states(self, providers: tuple[str, ...], *, within: float) -> dict[str, LoginState]:
        """Whether each of ``providers``' CLIs is signed in, for those answering within ``within``.

        Every CLI is probed at once, each on its own worker; one that has not answered when
        ``within`` seconds have passed is left out, and its probe ends on its own bound in the
        background. A probe that fails is left out too. Never raises.
        """
        found: dict[str, LoginState] = {}
        lock = self._threads.lock()

        def probe(provider: str) -> None:
            state = self._login(provider).state
            with lock:
                found[provider] = state

        workers = [
            self._threads.spawn(partial(probe, provider), name=f"login-probe-{provider}")
            for provider in providers
        ]
        deadline = self._threads.now() + within
        for worker in workers:
            worker.join(max(0.0, deadline - self._threads.now()))
        with lock:
            return dict(found)

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
        # > Provider profiles remain authoritative: `vs_agent.api` owns the provider facts and
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


# --- starting a harness's own login ------------------------------------------------------
#
# `LoginStarter.start` runs the login command the agent library names (agentshim's facts, the
# choice of variant in `vs_agent`) as a child that outlives the request: a device-code login
# prints a page to open and a code to enter, then waits for the person to finish. The starter
# reads the output until it has that instruction, answers, and leaves the child running; a
# worker waits for the child so it is reaped, and ends it if the person never finishes.

_LOGIN_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LOGIN_URL = re.compile(r"https?://[^\s\x1b\"'<>)]+")
_LOGIN_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")
_LOGIN_LINE_MAX = 300


def login_instruction(url: str | None, code: str | None) -> str | None:
    """What the person is asked to do, in one line, or ``None`` when nothing was shown.

    A page without a code is just the page. An instruction that would not fit a wire line is
    dropped rather than cut: half a URL is worse than none.
    """
    if url is None:
        return None
    line = f"Open {url} and enter the code {code}" if code is not None else f"Open {url}"
    return line if len(line) <= _LOGIN_LINE_MAX else None


class _Output:
    """What a login's output has shown so far; the reader writes, ``start`` waits on it."""

    def __init__(self, threads: Threads) -> None:
        self.condition = threads.condition()
        self.url: str | None = None
        self.code: str | None = None
        self.first_line = ""
        self.ended = False
        self.lines = 0

    def take(self, line: str) -> None:
        text = _LOGIN_ANSI.sub("", line).strip()
        with self.condition:
            self.lines += 1
            if text and not self.first_line:
                self.first_line = text[:_LOGIN_LINE_MAX]
            if self.url is None and (found := _LOGIN_URL.search(text)) is not None:
                self.url = found.group(0)
            if self.code is None and (found := _LOGIN_CODE.search(text)) is not None:
                self.code = found.group(0)
            self.condition.notify_all()

    def end(self) -> None:
        with self.condition:
            self.ended = True
            self.condition.notify_all()


@dataclass(frozen=True, slots=True)
class LoginBounds:
    """How long a login is waited for, in seconds of ``Threads``' clock."""

    page_wait: float = 10.0
    """The wait for the first page the login shows."""
    code_wait: float = 1.0
    """The further wait for a code once a page is shown."""
    lifetime: float = 900.0
    """How long a login may stay open before it is ended."""


class HeldLogin:
    """A login that stays up until it ends or ``cancel`` stops its process group."""

    def __init__(
        self,
        threads: Threads,
        result: LoginStarted | LoginUnavailable,
        process: PipedProcess | None,
    ) -> None:
        """Remember ``result`` and, when ``process`` is running, when it has been reaped."""
        self.result = result
        self._process = process
        self._finished = threads.event()
        if process is None:
            self._finished.set()

    def cancel(self) -> None:
        """Stop the login's process group. A login that never started is unchanged."""
        process = self._process
        if process is not None:
            process.terminate_group()

    def wait(self) -> None:
        """Block until the login process has been reaped."""
        self._finished.wait()

    def mark_finished(self) -> None:
        """Record that the process group has been reaped."""
        self._finished.set()


class LoginStarter:
    """Runs a login command and reports what it asked the person to do."""

    def __init__(
        self,
        processes: PipedProcessLauncher,
        threads: Threads,
        environment: Callable[[], Mapping[str, str]],
        bounds: LoginBounds,
    ) -> None:
        """Start commands through ``processes``, in the environment ``environment()`` gives them.

        The environment is read when a login starts, not here: it is the login shell's, which a
        helper that never starts a login has no reason to load. ``threads`` is the clock waited on.
        """
        self._processes = processes
        self._threads = threads
        self._environment = environment
        self._bounds = bounds

    def start(self, argv: tuple[str, ...]) -> LoginStarted | LoginUnavailable:
        """Run ``argv`` and answer once it has shown its page and code, ended, or run out of time."""
        return self.hold(argv).result

    def refuse(self, result: LoginUnavailable) -> HeldLogin:
        """A login that was not started, already finished."""
        return HeldLogin(self._threads, result, None)

    def hold(self, argv: tuple[str, ...]) -> HeldLogin:
        """Start ``argv`` and keep the child until ``HeldLogin.cancel`` or its lifetime ends."""
        try:
            process = self._processes.start(argv, env=self._environment(), merge_stderr=True)
        except OSError as error:
            return HeldLogin(
                self._threads, _failed(f"could not run {argv[0]}: {error.strerror or error}"), None
            )
        output = _Output(self._threads)
        held = HeldLogin(self._threads, LoginStarted(instruction=None), process)
        self._threads.spawn(lambda: _read(process, output), name="login-output")
        self._threads.spawn(
            lambda: _reap(process, self._bounds.lifetime, held.mark_finished),
            name="login-reaper",
        )
        with output.condition:
            output.condition.wait_for(
                lambda: output.url is not None or output.ended, self._bounds.page_wait
            )
            if output.url is not None:
                output.condition.wait_for(
                    lambda: output.code is not None or output.ended, self._bounds.code_wait
                )
            instruction = login_instruction(output.url, output.code)
            if instruction is None and output.url is None and output.ended and output.first_line:
                held.result = _failed(output.first_line)
                return held
        held.result = LoginStarted(instruction=instruction)
        return held


def _failed(detail: str) -> LoginUnavailable:
    return LoginUnavailable(
        reason=LoginUnavailableReason.START_FAILED, detail=detail.splitlines()[0][:_LOGIN_LINE_MAX]
    )


def _read(process: PipedProcess, output: _Output) -> None:
    """Feed the child's output lines to ``output`` until its pipe closes."""
    try:
        while line := process.stdout.readline():
            output.take(line)
    finally:
        output.end()


def _reap(process: PipedProcess, lifetime: float, finished: Callable[[], None]) -> None:
    """Wait for the child so it is reaped, and end its group if it outlives ``lifetime``."""
    try:
        process.wait(lifetime)
    except ProcessTimeoutError:
        process.terminate_group()
        try:
            process.wait(5.0)
        except ProcessTimeoutError:
            process.kill_group()
            process.wait(5.0)
    finally:
        finished()


def host_logins(processes: PipedProcessLauncher, threads: Threads) -> AgentLibraryLogins:
    """Wire this host's provider facts and bounded login process ownership on first use."""
    return AgentLibraryLogins(
        provider_login_here,
        provider_login_command_here,
        LoginStarter(processes, threads, launch_environment, LoginBounds()),
        threads,
    )


__all__ = [
    "AccountLogin",
    "AgentLibraryLogins",
    "AgentLogin",
    "AgentLoginsListReply",
    "AgentLoginsListRequest",
    "AgentLoginsStartReply",
    "AgentLoginsStartRequest",
    "ApiKeyLogin",
    "HeldLogin",
    "LoginBounds",
    "LoginHow",
    "LoginStartResult",
    "LoginStarted",
    "LoginStarter",
    "LoginState",
    "LoginUnavailable",
    "LoginUnavailableReason",
    "harness_binaries",
    "host_logins",
    "launch_environment",
    "login_instruction",
]
