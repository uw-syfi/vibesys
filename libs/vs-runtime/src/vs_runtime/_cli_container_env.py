"""Container environment and staged auth for a CLI agent provider."""

from __future__ import annotations

from vs_agent.api import (
    DOCKER_PROVIDER_ENV,
    AgentBackend,
    auth_copy_paths,
    auth_env_passthrough,
    auth_env_vars,
    auth_paths,
)


def cli_container_env(
    agent_backend: str | None, cli_provider: str | None
) -> tuple[str, dict[str, str]] | None:
    """Return ``(provider, container env)`` when a CLI provider needs a container.

    Every containerized environment (Docker, Modal, SkyPilot) now starts from
    a prebuilt agent image and installs nothing at container start, so this
    is the whole of what a CLI provider needs from the run request: the
    auth-presence check and the auth env passthrough. What used to be the
    shell-command half of this (:func:`vs_agent.cli_docker
    .docker_init_commands`, run through ``extra_init_commands``) is gone; see
    :func:`cli_provider_env_and_auth_files` for the staged-file counterpart
    Modal and SkyPilot pass through ``auth_files`` instead, matching the
    plain Docker path.

    Returns ``None`` when the run is not a containerized CLI agent (a
    different agent backend, or no CLI provider selected).

    Raises:
        ValueError: if *request.cli_provider* has neither a staged auth file
            nor a usable auth environment variable on this host.
    """
    effective_agent = agent_backend or AgentBackend.CLI
    if effective_agent != "cli" or not cli_provider:
        return None
    provider = cli_provider
    auth_env = auth_env_passthrough(provider)
    staged_auth = [spec for spec in auth_paths(provider) if spec.host_path.exists()]
    if not staged_auth and not auth_env:
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
    env = dict(DOCKER_PROVIDER_ENV.get(provider, {}))
    # Container processes inherit only what ``docker run -e`` sets; the editor
    # container has no other view of the host environment.
    env.update(auth_env)
    return provider, env


def cli_provider_env_and_auth_files(
    agent_backend: str | None, cli_provider: str | None
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return the container env and staged auth copies for a CLI provider, if any.

    The shared counterpart to the Docker environment's own inline
    version of this: every environment that starts a container from the
    prebuilt agent image copies auth the same way (via
    :func:`vs_agent.cli_docker.auth_copy_paths`, handed to the sandbox
    as ``auth_files`` so it copies them in at start), rather than running
    shell commands built from a provider's install recipe.
    """
    resolved_cli = cli_container_env(agent_backend, cli_provider)
    if resolved_cli is None:
        return {}, []
    provider, cli_provider_env = resolved_cli
    return cli_provider_env, auth_copy_paths(provider)
