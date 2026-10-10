"""Launch ``agentshim.Session`` objects under VibeSys execution policy.

VibeSys consumes the ``agentshim`` library only through its public API. The
library owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags); this module owns the VibeSys policy of how a
session is launched: sandbox confinement, the session environment, which
transport a provider uses, and the operator-isolation scopes.

:class:`SessionLauncher` is the I/O seam :class:`~vs_agent.client.AgentClient`
opens sessions through. :class:`ConfinedSessionLauncher` is the production
implementation; ``vs_agent.api.testing`` supplies a scripted one.

One path runs every session, host or container: build (or look up) a
:class:`~vs_sandbox.WorkspaceSandbox`, wrap a plain ``HostCommandExecutor`` to
confine it, and hand the library the sandbox's own environment. Every path the
agent is told about -- the schema directory, an MCP server's command -- goes
through :meth:`~vs_sandbox.WorkspaceSandbox.agent_path`, so nothing here names
a backend.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from weakref import WeakSet

import agentshim

from vs_agent import shim_translation
from vs_agent.contracts import AgentCapabilities, AgentSpawnError
from vs_agent.docker_confinement import DockerContainerConfinement
from vs_agent.host_resource_declarations import (
    declare_agent_host_resources,
    prepare_provider_state,
)
from vs_agent.provider_policy import SHIPPED_PROVIDERS
from vs_agent.session_environment import (
    dropped_launcher_names,
    session_environment,
    validate_env_names,
)
from vs_agent.shim_turns import LaunchedSession, SteerLedger, TurnEvents
from vs_sandbox.api import build_host_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_agent.contracts import AgentSessionSpec, ProviderReadiness
    from vs_sandbox.api import DockerSandbox, WorkspaceSandbox

AGENTSHIM_CAPABILITIES = AgentCapabilities(
    tool_servers=True,
    nested_read_only_paths=True,
    hidden_paths=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""Capabilities invariant across host and container execution.

``provider_session_resume``, ``skill_isolation``, ``mcp_isolation`` and
``config_isolation`` are narrowed per provider from
:attr:`agentshim.ProviderProfile.supports_resume`,
:attr:`agentshim.ProviderProfile.skill_scopes`,
:attr:`agentshim.ProviderProfile.mcp_scopes` and
:attr:`agentshim.ProviderProfile.config_scopes` when the launcher is built.
"""

_HOST_BINARY_CHECK_TIMEOUT_S = 15.0
"""agentshim's own default: a host binary answers ``--help`` immediately."""

_CONTAINER_BINARY_CHECK_TIMEOUT_S = 60.0
"""How long a container's ``<binary> --help`` may take before it counts as dead.

The check crosses a ``docker exec``, so it waits on the daemon as well as the
CLI. A daemon busy starting or stopping other containers regularly takes tens
of seconds to attach, and a false negative here ends the run (see
``docs/contributing/agent-drivers.md``), so the container budget is four times
the host one.
"""

TRANSIENT_RETRY_DELAYS_S: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0, 480.0)
"""Waits in seconds before each retry of a turn that failed on a transient provider error.

agentshim classifies the failure (``agentshim.FailureKind.TRANSIENT``: an
overload, a rate limit, or a server error) and its ``Session`` waits these
delays (``agentshim.RetryPolicy``). The provider CLI has already retried inside
the turn before it exits, so by then the outage has lasted minutes; these waits
add about fifteen more before the error reaches the run, which otherwise ends
on the first one.
"""


def supported_providers() -> list[str]:
    """Return the sorted provider names VibeSys can launch."""
    return sorted(SHIPPED_PROVIDERS)


def _ignore_log(_message: str) -> None:
    """Discard a diagnostic when no log sink was configured."""


class SessionLauncher(Protocol):
    """Opens configured ``agentshim`` sessions under VibeSys execution policy."""

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this launcher's execution system can enforce."""
        ...

    def launch(self, spec: AgentSessionSpec) -> LaunchedSession:
        """Open a session or reject any unsupported part of ``spec``."""
        ...

    def close(self) -> None:
        """Release launcher resources. Implementations must be idempotent."""
        ...


class ExecutorFactory(Protocol):
    """Builds the unconfined command executor :func:`confine_to_sandbox` wraps."""

    def __call__(self) -> agentshim.CommandExecutor:
        """Return a fresh, unconfined executor for one session."""
        ...


class _ConfinableSandbox(Protocol):
    """The shape :func:`confine_to_sandbox` needs, real or a test double alike.

    Both ``vs_sandbox.WorkspaceSandbox`` (a host confinement policy) and
    ``vs_sandbox.DockerSandbox`` satisfy this structurally, and so does any
    lightweight double a test builds for either: nothing here requires the
    concrete class.
    """

    def wrap(self, argv: list[str], cwd: Path | str | None = None, /) -> list[str]:
        """Return *argv* rewritten to run confined to one workspace."""
        ...

    def agent_path(self, path: Path | str, /) -> str:
        """Return the path the confined process sees for *path*."""
        ...

    @property
    def env(self) -> Mapping[str, str]:
        """Return the environment variables the confined process runs with."""
        ...


def confine_to_sandbox(
    executor: agentshim.CommandExecutor,
    sandbox: _ConfinableSandbox,
    *,
    find_binary: Callable[[str, Mapping[str, str]], str] | None = None,
) -> agentshim.CommandExecutor:
    """Return *executor* with every command rewritten through *sandbox*.

    This is the one chokepoint through which the provider CLI launches, for a
    host confinement policy and an already-running Docker sandbox alike:
    *sandbox* supplies the ``wrap`` call, so the caller never branches on which
    kind of sandbox it was given. Every ``wrap`` implementation accepts the
    request's ``cwd``; a host sandbox confines to exactly one workspace
    already and ignores it, while a Docker sandbox serves every turn from one
    container regardless of working directory and maps the argument to ``-w``.

    *find_binary*, when given, replaces the default host-side lookup
    (``shutil.which`` against the request's own environment). A Docker
    sandbox's ``env`` carries the *container's* ``PATH``, which resolves to
    nothing on this host, so its caller passes an override that trusts the
    bare name to the far side of ``docker exec`` instead.
    """

    def transform(request: agentshim.CommandRequest) -> agentshim.CommandRequest:
        return replace(request, argv=sandbox.wrap(list(request.argv), request.cwd))

    return agentshim.TransformingExecutor(executor, transform, find_binary=find_binary)


def build_host_executor(sandbox: WorkspaceSandbox | None) -> agentshim.CommandExecutor:
    """Run on this host, confined to *sandbox* when one is given.

    A thin convenience over :func:`confine_to_sandbox` for callers (notably
    the host-confinement test suite) that want the launcher's own default
    executor policy without going through :class:`ConfinedSessionLauncher` itself.
    """
    host = agentshim.HostCommandExecutor()
    return host if sandbox is None else confine_to_sandbox(host, sandbox)


def _resolve_binary_path(binary: str, env: Mapping[str, str]) -> str | None:
    """Locate *binary* for the host resource declaration, or ``None`` if absent.

    The declaration is built before the agent exists because the sandbox it
    feeds is what the agent's executor is built from. A missing binary is not
    reported here: constructing the agent raises the library's own
    ``CliNotFoundError`` with the provider's name attached.
    """
    try:
        return _find_host_binary(binary, env)
    except agentshim.CliNotFoundError:
        return None


def _find_host_binary(binary: str, env: Mapping[str, str]) -> str:
    """Resolve a host CLI symlink before passing its path into confinement."""
    path = agentshim.HostCommandExecutor().find_binary(binary, env)
    return str(Path(path).resolve())


def _bare_binary_name(name: str, env: Mapping[str, str]) -> str:
    """Trust *name* to resolve on the far side of ``docker exec``.

    A host-side lookup against a container's own ``PATH`` would search
    directories that only exist inside the image; the container's shell
    resolves its own binaries once the wrapped command actually runs there.
    """
    del env
    return name


def _skill_scope(profile: agentshim.ProviderProfile) -> agentshim.SkillScope:
    """Offer a session only the run's skills wherever the provider can enforce it.

    A run's behavior must not depend on who launches it, so the operator's
    personal and plugin skills stay out (``SkillScope.PROJECT``). A provider
    with no mechanism for that keeps ``ALL``; the launcher reports it through
    ``AgentCapabilities.skill_isolation`` and logs it per session rather than
    refusing to run.
    """
    if agentshim.SkillScope.PROJECT in profile.skill_scopes:
        return agentshim.SkillScope.PROJECT
    return agentshim.SkillScope.ALL


def _mcp_scope(profile: agentshim.ProviderProfile) -> agentshim.McpScope:
    """Connect a session only to the MCP servers the run configured.

    The operator's own MCP servers (user or project configuration, plugins,
    account connectors) must not change what a run's agents can call, so a
    session gets ``McpScope.SESSION`` wherever the provider can enforce it. A
    provider with no mechanism keeps ``ALL``; the launcher reports it through
    ``AgentCapabilities.mcp_isolation`` and logs it per session rather than
    refusing to run.
    """
    if agentshim.McpScope.SESSION in profile.mcp_scopes:
        return agentshim.McpScope.SESSION
    return agentshim.McpScope.ALL


def _config_scope(profile: agentshim.ProviderProfile, *, has_home: bool) -> agentshim.ConfigScope:
    """Keep the operator's own CLI configuration out of a session where possible.

    Settings, hooks, global instructions, notify commands and memory in the
    operator's provider state must not change what a run's agents do, so a
    session gets ``ConfigScope.PROJECT`` wherever the provider can enforce
    it. A provider that can only enforce it in a dedicated state root
    (``profile.config_home_files``) needs *has_home*: a run-owned host home
    the launcher can prepare. Otherwise the session keeps ``ALL``; the launcher
    reports it through ``AgentCapabilities.config_isolation`` and logs it per
    session rather than refusing to run.
    """
    if agentshim.ConfigScope.PROJECT not in profile.config_scopes:
        return agentshim.ConfigScope.ALL
    if profile.config_home_files and not has_home:
        return agentshim.ConfigScope.ALL
    return agentshim.ConfigScope.PROJECT


def _without_stale_pwd(env: Mapping[str, str]) -> dict[str, str]:
    """Drop ``PWD`` so the CLI trusts its real working directory.

    A subprocess cwd does not rewrite ``$PWD``, and bun-based CLIs (opencode)
    trust ``$PWD`` over the real cwd for workspace config discovery, so a
    stale value makes them miss ``<workspace>/opencode.json``.
    """
    return {key: value for key, value in env.items() if key != "PWD"}


@dataclass(frozen=True, slots=True)
class _Launch:
    """What one session (or readiness probe) runs the provider CLI with."""

    executor: agentshim.CommandExecutor
    env: Mapping[str, str]
    sandbox: _ConfinableSandbox | None
    config_scope: agentshim.ConfigScope
    transport: agentshim.TransportKind
    confinement: agentshim.Confinement | None = None
    """Set for a long-lived transport in a container: agentshim confines, maps and reaps.

    ``executor`` is then the plain executor and ``env`` is unused (the
    confinement supplies the environment). Without it the executor is already
    confined and ``env`` is the agent's environment.
    """


class ConfinedSessionLauncher:
    """Launch agentshim sessions confined by VibeSys sandbox policy."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-010139 [PLR0913]; Preserve ConfinedSessionLauncher.__init__'s named-argument contract because callers pass these independent settings directly.
        self,
        *,
        provider: str,
        timeout: int | None = None,
        docker_sandboxes: dict[str, DockerSandbox] | None = None,
        workspace_sandboxes: Callable[[Path], DockerSandbox | None] | None = None,
        log: Callable[[str], None] | None = None,
        executor_factory: ExecutorFactory | None = None,
        check_timeout: float | None = None,
        transient_retry_delays: Sequence[float] = TRANSIENT_RETRY_DELAYS_S,
        agent_homes: Path | None = None,
        env_passthrough: Sequence[str] = (),
        launcher_env: Callable[[], Mapping[str, str]] = agentshim.interactive_env,
        transport: agentshim.TransportKind | None = None,
        clock: agentshim.Clock | None = None,
        ids: agentshim.IdAllocator | None = None,
    ) -> None:
        """Configure one provider; ``executor_factory`` replaces the base executor.

        ``transport`` fixes how every session reaches the provider. Left as
        ``None`` it is derived: a container session of a provider that
        agentshim lists in ``stream_provider_names()`` keeps one long-lived
        process per conversation (``TransportKind.STREAM``); every other
        session runs one process per turn (``TransportKind.ONE_SHOT``).

        ``agent_homes`` is the run's root for dedicated provider CLI homes
        (one subdirectory per provider, shared by every session the run opens
        so a conversation resumes across candidates). A host session of a
        provider that keeps the operator's configuration in its state root
        runs against that home; see :func:`_config_scope`.

        ``launcher_env`` reads the environment VibeSys was launched with (by
        default the interactive login shell's); a session inherits only the
        allowlisted part of it plus ``env_passthrough`` names (see
        :mod:`vs_agent.session_environment`).

        ``workspace_sandboxes`` answers, for a session's workspace, the sandbox
        that mounts it, or ``None`` when the role's sandbox serves it. A client
        shared by turns in several workspaces needs it: each workspace has its
        own container, and ``docker exec`` into another one cannot reach the
        workspace's directory.

        ``docker_sandboxes`` maps a session's role to an already-started
        :class:`~vs_sandbox.DockerSandbox` (built and started by the run
        environment, not by this launcher). Its presence is what selects
        container execution: every session this launcher opens then looks up
        its sandbox there instead of building a host one.

        ``check_timeout`` bounds the one-off ``<binary> --help`` health check
        each session runs before its first turn. It defaults to the execution
        mode's budget: a container check crosses a ``docker exec`` and is given
        four times as long as a host one.

        ``clock`` and ``ids`` replace agentshim's wall clock and random turn ids,
        so a test measures turn timeouts and retry waits on a fake clock and
        names turns reproducibly; production leaves them ``None``.

        ``transient_retry_delays`` are the waits before each retry of a turn
        that failed on a transient provider error; see
        :data:`TRANSIENT_RETRY_DELAYS_S`.
        """
        if provider not in SHIPPED_PROVIDERS:
            message = (
                f"unknown AgentShim provider {provider!r}; expected one of: {supported_providers()}"
            )
            raise ValueError(message)
        self._provider = provider
        self._timeout = timeout
        self._docker_sandboxes = docker_sandboxes
        self._workspace_sandboxes = workspace_sandboxes
        self._log = log or _ignore_log
        self._executor_factory: ExecutorFactory = executor_factory or agentshim.HostCommandExecutor
        self._check_timeout = (
            check_timeout
            if check_timeout is not None
            else (
                _CONTAINER_BINARY_CHECK_TIMEOUT_S
                if docker_sandboxes is not None
                else _HOST_BINARY_CHECK_TIMEOUT_S
            )
        )
        self._transient_retry_delays = tuple(transient_retry_delays)
        self._agent_homes = agent_homes
        self._env_passthrough = validate_env_names(env_passthrough)
        self._dropped_names_logged = False
        self._dropped_names_lock = threading.Lock()
        self._launcher_env = launcher_env
        self._transport = transport
        self._clock = clock
        self._ids = ids
        self._sessions: WeakSet[LaunchedSession] = WeakSet()
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe the policy and lifecycle features this launcher enforces."""
        return replace(
            AGENTSHIM_CAPABILITIES,
            host_path_grants=self._docker_sandboxes is None,
            container_execution=self._docker_sandboxes is not None,
            provider_session_resume=(
                agentshim.get_provider(self._provider).profile.supports_resume
            ),
            skill_isolation=_skill_scope(agentshim.get_provider(self._provider).profile)
            is agentshim.SkillScope.PROJECT,
            mcp_isolation=_mcp_scope(agentshim.get_provider(self._provider).profile)
            is agentshim.McpScope.SESSION,
            config_isolation=self._config_scope_for(agentshim.get_provider(self._provider).profile)
            is agentshim.ConfigScope.PROJECT,
        )

    def _config_scope_for(self, profile: agentshim.ProviderProfile) -> agentshim.ConfigScope:
        # A container keeps its own state root inside the container, which a
        # host-side home cannot replace.
        has_home = self._agent_homes is not None and self._docker_sandboxes is None
        return _config_scope(profile, has_home=has_home)

    def launch(self, spec: AgentSessionSpec) -> LaunchedSession:
        """Open a session, classifying process setup failures as retryable faults."""
        try:
            return self._launch(spec)
        except (OSError, ImportError, agentshim.CliNotFoundError, agentshim.CliCheckError) as exc:
            raise AgentSpawnError(spec.provider, str(exc)) from exc

    def probe_readiness(self, spec: AgentSessionSpec) -> ProviderReadiness:
        """Report whether the CLI *spec* would launch is installed and logged in.

        The probe runs on the executor, confinement and environment
        :meth:`launch` builds for the same *spec* (one code path,
        :meth:`_launch_for`), so a container or sandbox is probed where the
        agent would run. No model is called. A missing binary is a result,
        not an exception; the caller decides what a problem means.
        """
        try:
            launch = self._launch_for(spec)
        except (OSError, ImportError) as exc:
            raise AgentSpawnError(spec.provider, str(exc)) from exc
        status = agentshim.probe_provider(
            spec.provider,
            executor=launch.executor,
            confinement=launch.confinement,
            env=None if launch.confinement is not None else launch.env,
            timeout=self._check_timeout,
        )
        return shim_translation.readiness_from(status)

    def _launch_for(self, spec: AgentSessionSpec) -> _Launch:
        """Validate *spec* and build the executor, environment and sandbox it runs with.

        Every session takes the same route: look up or build the sandbox for
        this role, confine a fresh executor to it, and hand the library the
        sandbox's own environment.
        """
        if self._closed:
            message = "session launcher is closed"
            raise RuntimeError(message)
        if spec.provider != self._provider:
            message = f"launcher for {self._provider!r} cannot open a {spec.provider!r} session"
            raise ValueError(message)
        in_container = self._docker_sandboxes is not None
        if spec.policy.containerized != in_container:
            message = (
                "agent session container policy does not match the configured "
                "AgentShim execution mode"
            )
            raise ValueError(message)

        provider = agentshim.get_provider(spec.provider)
        config_scope = self._config_scope_for(provider.profile)
        sandbox, find_binary, host_env = self._sandbox_for(spec, config_scope)
        transport = self._transport_for(spec)

        if transport is agentshim.TransportKind.STREAM and in_container:
            # agentshim confines the long-lived process itself, so it can mark
            # it for `reap` and map the working directory, MCP commands and
            # schema directory the way the container sees them.
            confinement = DockerContainerConfinement(
                self._docker_sandbox_for(spec), runner=self._executor_factory()
            )
            return _Launch(
                executor=self._executor_factory(),
                env=confinement.env,
                sandbox=sandbox,
                config_scope=config_scope,
                transport=transport,
                confinement=confinement,
            )

        executor: agentshim.CommandExecutor = self._executor_factory()
        if sandbox is not None:
            executor = confine_to_sandbox(executor, sandbox, find_binary=find_binary)
        env = sandbox.env if sandbox is not None else host_env
        return _Launch(
            executor=executor,
            env=env,
            sandbox=sandbox,
            config_scope=config_scope,
            transport=transport,
        )

    def _transport_for(self, spec: AgentSessionSpec) -> agentshim.TransportKind:
        """Choose the transport from the execution mode and agentshim's registry."""
        if self._transport is not None:
            return self._transport
        if spec.policy.containerized and spec.provider in agentshim.stream_provider_names():
            return agentshim.TransportKind.STREAM
        return agentshim.TransportKind.ONE_SHOT

    def _launch(self, spec: AgentSessionSpec) -> LaunchedSession:
        """Open one configured agentshim conversation."""
        launch = self._launch_for(spec)
        sandbox = launch.sandbox
        config_scope = launch.config_scope
        steers = SteerLedger()
        event_handler = TurnEvents(steers)
        agent = agentshim.Agent(
            spec.provider,
            model=spec.model,
            executor=launch.executor,
            confinement=launch.confinement,
            transport=launch.transport,
            permissions=agentshim.NativePermissions.bypass(),
            approvals=agentshim.ApprovalPolicy.DENY,
            retry=agentshim.RetryPolicy(delays=self._transient_retry_delays),
            event_handlers=[event_handler],
            env=None if launch.confinement is not None else launch.env,
            log=self._log,
            check_timeout=self._check_timeout,
            clock=self._clock,
            ids=self._ids,
        )
        skill_scope = _skill_scope(agent.profile)
        if skill_scope is not agentshim.SkillScope.PROJECT:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own skills; "
                "this session is offered them beside the run's"
            )
        mcp_scope = _mcp_scope(agent.profile)
        if mcp_scope is not agentshim.McpScope.SESSION:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own MCP servers; "
                "this session is connected to them beside the run's"
            )
        if config_scope is not agentshim.ConfigScope.PROJECT:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own CLI configuration; "
                "this session loads their settings, hooks and global instructions"
            )
        agent_path: Callable[[str], str] = (
            shim_translation.default_agent_path
            # agentshim maps paths itself when it confines the process.
            if launch.confinement is not None or sandbox is None
            else sandbox.agent_path
        )
        # A conversation's MCP servers are fixed when it opens: a stream
        # provider (Codex's app-server) refuses a turn that names a different
        # set, so they ride the session and a turn never resends them.
        session = LaunchedSession(
            session=agent.session(
                str(spec.workspace),
                mcp_servers=tuple(
                    shim_translation.mcp_server_from(
                        server, agent_path, pin_interpreter=not spec.policy.containerized
                    )
                    for server in spec.mcp_servers
                ),
                skill_scope=skill_scope,
                mcp_scope=mcp_scope,
                config_scope=config_scope,
            ),
            profile=agent.profile,
            spec=spec,
            events=event_handler,
            steers=steers,
            agent_path=agent_path,
            timeout=self._timeout,
            log=self._log,
        )
        self._sessions.add(session)
        return session

    def _sandbox_for(
        self,
        spec: AgentSessionSpec,
        config_scope: agentshim.ConfigScope,
    ) -> tuple[
        WorkspaceSandbox | None,
        Callable[[str, Mapping[str, str]], str] | None,
        dict[str, str],
    ]:
        """Return the sandbox this session confines to, its binary lookup, and host env.

        The host environment (empty for a container session) is what the
        session runs with when host confinement came back ``None``.

        A container session's sandbox already exists, started by the run
        environment; a host session's is built fresh from the declared
        resources (and may come back ``None`` where confinement is
        unavailable or disabled). A container's binary lookup trusts the bare
        name to ``docker exec``'s own ``PATH`` instead of searching this host,
        which the container's environment does not describe.
        """
        if self._docker_sandboxes is not None:
            return self._docker_sandbox_for(spec), _bare_binary_name, {}
        env = self._unconfined_host_env(spec, config_scope)
        return self._host_sandbox(spec, env), _find_host_binary, env

    def _unconfined_host_env(
        self, spec: AgentSessionSpec, config_scope: agentshim.ConfigScope
    ) -> dict[str, str]:
        """Return the host session environment before any sandbox is applied.

        The allowlisted launcher environment, the run's own variables, and,
        for ``ConfigScope.PROJECT``, the variables that point the CLI at the
        run's dedicated home (which this call prepares). Used both to build
        the host sandbox (whose own ``env`` then reflects it) and as the
        session environment when confinement came back unavailable.
        """
        profile = agentshim.get_provider(spec.provider).profile
        launcher = self._launcher_env()
        self._log_dropped_names_once(launcher, profile)
        env = session_environment(
            launcher,
            profile=profile,
            passthrough=self._env_passthrough,
            run=dict(spec.environment),
        )
        if config_scope is agentshim.ConfigScope.PROJECT and self._agent_homes is not None:
            home = self._agent_homes / spec.provider
            env.update(agentshim.prepare_config_home(profile, home, env))
        prepare_provider_state(env, profile=profile)
        return _without_stale_pwd(env)

    def _log_dropped_names_once(
        self, launcher: Mapping[str, str], profile: agentshim.ProviderProfile
    ) -> None:
        """Log, once per launcher (one run), which launcher variables sessions do not inherit.

        Names only, never values. An operator who needs one adds it to
        ``[agent] env_passthrough``.
        """
        with self._dropped_names_lock:
            if self._dropped_names_logged:
                return
            self._dropped_names_logged = True
        dropped = dropped_launcher_names(
            launcher, profile=profile, passthrough=self._env_passthrough
        )
        if dropped:
            self._log(
                "[env] agent sessions do not inherit these launcher variables "
                f"(add names to [agent] env_passthrough to pass them): {', '.join(dropped)}"
            )

    def _host_sandbox(
        self,
        spec: AgentSessionSpec,
        env: Mapping[str, str],
    ) -> WorkspaceSandbox | None:
        profile = agentshim.get_provider(spec.provider).profile
        resources = declare_agent_host_resources(
            env,
            binary_path=_resolve_binary_path(profile.binary, env),
            provider=spec.provider,
            additional=spec.policy.host_resources,
        )
        return build_host_sandbox(
            spec.workspace,
            env=dict(env),
            resources=resources,
            log=self._log,
            project_path_policy=spec.policy.project_paths,
            require_enforcement=spec.policy.require_enforcement,
        )

    def _docker_sandbox_for(self, spec: AgentSessionSpec) -> DockerSandbox:
        if self._docker_sandboxes is None:
            message = "container execution has no Docker sandbox registry"
            raise AssertionError(message)
        if self._workspace_sandboxes is not None:
            workspace_sandbox = self._workspace_sandboxes(spec.workspace)
            if workspace_sandbox is not None:
                return workspace_sandbox
        sandbox = self._docker_sandboxes.get(spec.role)
        if sandbox is None:
            message = f"no AgentShim Docker sandbox configured for role {spec.role!r}"
            raise ValueError(message)
        return sandbox

    def close(self) -> None:
        """Close every session opened by this launcher, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()
