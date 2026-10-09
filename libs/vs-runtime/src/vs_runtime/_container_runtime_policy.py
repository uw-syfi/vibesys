"""Run-environment policy for a task's ``docker_in_docker`` request."""

from __future__ import annotations


class DockerInDockerUnsupportedError(ValueError):
    """A docker_in_docker task was paired with an environment that cannot honor it."""

    @classmethod
    def environment(cls, environment: str) -> DockerInDockerUnsupportedError:
        """Describe the declaring key and the environment that cannot run it."""
        return cls(
            f"the task declares [environment] docker_in_docker = true, which only the Docker "
            f"run environment can provide (a sandbox container with its own Docker daemon "
            f"under Sysbox); the {environment} run environment cannot. Run it with --docker "
            f"on a host with Sysbox."
        )


def reject_docker_in_docker(*, docker_in_docker: bool, environment: str) -> None:
    """Refuse a docker_in_docker request on an environment that cannot honor it.

    Silently running a container-topology task without its own daemon would
    produce a candidate that cannot build, so the combination is an error.
    """
    if docker_in_docker:
        raise DockerInDockerUnsupportedError.environment(environment)


def attaches_accelerator(*, docker_in_docker: bool) -> bool:
    """Return whether the sandbox may be given the backend's accelerators.

    Sysbox cannot forward GPUs or devices, and a container topology does not
    need them, so a docker-in-docker sandbox is CPU-only.
    """
    return not docker_in_docker


DOCKER_IN_DOCKER_NOTICE = (
    "[docker] docker_in_docker: sysbox-runc with a per-run Docker daemon inside the "
    "sandbox; no accelerator is attached (Sysbox cannot forward devices)"
)
