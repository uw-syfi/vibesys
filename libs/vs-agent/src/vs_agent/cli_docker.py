"""Docker auth staging for CLI agent providers.

Provider facts come from ``agentshim``'s ``ProviderProfile`` at call time:
state directories, auth environment variables, and which files carry
credentials. What stays here is VibeSys policy that is not itself a provider
*decision*. The container environment table lives in
:mod:`vs_agent.provider_policy`; this module imports it.

Every containerized CLI path (plain Docker, and Modal/SkyPilot's local editor
container since #676) now starts from a prebuilt agent image with every
shipped CLI, toolchain, and the non-root ``agent`` user already installed, so
none of it needs a per-run shell-recipe install list any more: it copies auth
files itself at container start (see :func:`auth_copy_paths`) instead of
running shell commands through ``extra_init_commands``. The install-recipe
machinery this module used to carry for Modal and SkyPilot
(``docker_init_commands`` and its supporting override tables) is gone now
that those two also consume the agent image; provider CLI version pins live
in :mod:`vs_agent.provider_policy`, which owns provider decisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent import provider_profiles
from vs_agent.auth_policy import HostAuthUnplannable, plan_cli_auth, plan_host_cli_auth
from vs_agent.provider_policy import DOCKER_PROVIDER_ENV
from vs_sandbox.api import AGENT_HOME

# Re-exported for existing importers (``vs_agent.factory``,
# ``vs_runtime._run_environment``, and this module's own tests reach it as
# ``cli_docker.DOCKER_PROVIDER_ENV``); the value itself lives in
# ``provider_policy`` now.
__all__ = ["DOCKER_PROVIDER_ENV"]

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_agent.auth_policy import AgentAuthPolicy, CliAuthPlan


@dataclass(frozen=True)
class DockerAuthPath:
    """Host provider state and its writable location inside the container."""

    host_path: Path
    container_path: str
    #: Whether the file holds refreshable login credentials. Those are shared
    #: with the container writable; everything else is copied read-only.
    credential: bool = False


def auth_paths(provider: str) -> list[DockerAuthPath]:
    """Return the provider state files to stage into a container, in order.

    Settings and user configuration are mounted read-only under
    ``/opt/vibesys-auth`` and copied into the container's ephemeral writable
    layer before the CLI starts. Login credentials are instead mounted
    read-write at their final path: a CLI refreshes its OAuth tokens during a
    run, and a refresh that stays in the throwaway copy leaves the host login
    holding a refresh token the provider has already rotated. Which
    home-relative paths there are comes straight from
    ``ProviderProfile.auth_files`` (agentshim 0.6.1+), and which of them are
    credentials from ``ProviderProfile.credential_files`` (0.15.3+): each
    entry is either a state directory's leaf file or a state entry that is
    itself a single file, at the same relative path under the agent image's
    HOME. A state directory the profile does not list a leaf for contributes
    nothing.

    Raises:
        ValueError: if agentshim does not register *provider*.
    """
    home = Path.home()
    profile = provider_profiles.provider_profile(provider)
    return [
        DockerAuthPath(
            home / auth_file,
            f"{AGENT_HOME}/{auth_file}",
            credential=auth_file in profile.credential_files,
        )
        for auth_file in profile.auth_files
    ]


def auth_env_vars(provider: str) -> tuple[str, ...]:
    """Return the host environment variables that carry *provider* credentials.

    Forwarded into the container alongside the staged files above. A host may
    authenticate a CLI entirely through the environment — an
    ``ANTHROPIC_AUTH_TOKEN`` plus ``ANTHROPIC_BASE_URL`` pointing at a proxy or
    enterprise gateway is a first-class Claude Code auth mechanism, and plain
    API keys are another — in which case no provider state file exists to stage
    and the container CLI would start unauthenticated. Host sandboxes never hit
    this because they inherit the host environment directly.

    The profile lists credential and endpoint variables only. VibeSys owns
    per-role model selection, so a model-selection variable such as
    ``ANTHROPIC_MODEL`` must never appear here: forwarding a host export would
    let it silently override the configured model inside the container.

    Raises:
        ValueError: if agentshim does not register *provider*.
    """
    return provider_profiles.provider_profile(provider).auth_env_vars


def auth_env_passthrough(provider: str) -> dict[str, str]:
    """Return the host auth environment variables *provider* can actually use.

    Unset and blank variables are dropped: an empty host export carries no
    credential and must not shadow staged file authentication or make the
    preflight check believe the container is authenticated.
    """
    values: dict[str, str] = {}
    for name in auth_env_vars(provider):
        value = os.environ.get(name)
        if value and value.strip():
            values[name] = value
    return values


def auth_bind_mounts(provider: str) -> list[tuple[str, str, bool]]:
    """Return the ``(host, container, read_only)`` mounts for existing provider state.

    Settings are staged read-only under ``/opt/vibesys-auth``. A credential
    file is mounted read-write at its final path in the agent home, as a
    single file and never through its directory: ``~/.claude`` and ``~/.codex``
    also hold history and sessions that must stay in the container.

    A CLI that refreshes its login either rewrites the file in place or, like
    Claude Code, writes a temporary file and renames it over the target. The
    rename fails with ``EBUSY`` on a mounted file, and every CLI that renames
    falls back to rewriting in place, which reaches the host file.
    """
    out: list[tuple[str, str, bool]] = []
    for index, spec in enumerate(auth_paths(provider)):
        if not spec.host_path.exists():
            continue
        if spec.credential:
            out.append((str(spec.host_path), spec.container_path, False))
        else:
            out.append((str(spec.host_path), f"/opt/vibesys-auth/{index}", True))
    return out


def auth_copy_paths(provider: str) -> list[tuple[str, str]]:
    """Return ``(staged source, agent-home destination)`` pairs to copy at start.

    Handed straight to ``DockerSandbox(auth_files=...)``, which copies each
    pair in as a start-time step rather than running shell commands through
    ``extra_init_commands``. Indexing matches :func:`auth_bind_mounts`: entry
    *i*'s staging mount is ``/opt/vibesys-auth/{i}``. Credential files are not
    copied: they are mounted writable at their final path instead.
    """
    return [
        (f"/opt/vibesys-auth/{index}", spec.container_path)
        for index, spec in enumerate(auth_paths(provider))
        if spec.host_path.exists() and not spec.credential
    ]


@dataclass(frozen=True)
class CliAuthDelivery:
    """What a CLI agent's container is given to log in with.

    ``env`` is set with ``docker run -e``; ``bind_mounts`` are
    ``(host, container, read_only)``; ``copy_paths`` are the staged copies made
    at container start (see :func:`auth_copy_paths`). ``login`` is the plan the
    container gets exactly, from the run's auth policy or, without one, from
    how the CLI logs in on the host; or why the host's login cannot be
    planned, when the host's auth state is forwarded as it is.
    """

    env: Mapping[str, str]
    bind_mounts: tuple[tuple[str, str, bool], ...]
    copy_paths: tuple[tuple[str, str], ...]
    login: CliAuthPlan | HostAuthUnplannable

    def describe(self) -> str:
        """One log line naming where the container's CLI logs in from, never a value."""
        return self.login.describe()


def cli_auth_delivery(provider: str, policy: AgentAuthPolicy | None) -> CliAuthDelivery:
    """Return the credentials a container for *provider* gets, under *policy*.

    With a policy, the container gets exactly the plan's variables and files
    and nothing else of the provider's auth state, settings files included.

    Without one, agentshim plans what the container needs to log in the way
    the CLI does on this host (:func:`plan_host_cli_auth`), and the container
    gets exactly that: the operator's other logins are not lent out beside the
    one the CLI uses, and the host's settings (hooks, plugins, helper commands
    naming host paths) do not reach the agent. Only when agentshim cannot plan
    the host's login is every set auth variable and every existing auth file
    forwarded (see :func:`auth_paths`).

    Raises:
        ValueError: without a policy, when the host has no credential at all.
        vs_agent.auth_policy.AgentAuthError: with one, when it cannot be met.
    """
    if policy is not None:
        return _planned_delivery(plan_cli_auth(provider, policy, env=os.environ, home=Path.home()))
    host_login = plan_host_cli_auth(provider, env=os.environ, home=Path.home())
    if isinstance(host_login, HostAuthUnplannable):
        return _host_state_delivery(provider, host_login)
    return _planned_delivery(host_login)


def _host_state_delivery(provider: str, reason: HostAuthUnplannable) -> CliAuthDelivery:
    env = auth_env_passthrough(provider)
    mounts = tuple(auth_bind_mounts(provider))
    if not mounts and not env:
        checked_files = (
            ", ".join(str(spec.host_path) for spec in auth_paths(provider)) or "<none registered>"
        )
        checked_env = ", ".join(auth_env_vars(provider)) or "<none registered>"
        message = (
            f"no {provider!r} CLI authentication is available for the container: "
            f"none of the host files exist ({checked_files}) and none of the "
            f"environment variables are set ({checked_env}). Authenticate the "
            f"{provider} CLI on this host, or export one of those variables, "
            "before running in an isolated environment."
        )
        raise ValueError(message)
    return CliAuthDelivery(env, mounts, tuple(auth_copy_paths(provider)), login=reason)


def _planned_delivery(plan: CliAuthPlan) -> CliAuthDelivery:
    mounts: list[tuple[str, str, bool]] = []
    copies: list[tuple[str, str]] = []
    for index, staged in enumerate(plan.files):
        destination = f"{AGENT_HOME}/{staged.home_path}"
        if staged.refreshable:
            mounts.append((str(staged.host_path), destination, False))
        else:
            source = f"/opt/vibesys-auth/plan-{index}"
            mounts.append((str(staged.host_path), source, True))
            copies.append((source, destination))
    return CliAuthDelivery(dict(plan.env), tuple(mounts), tuple(copies), login=plan)
