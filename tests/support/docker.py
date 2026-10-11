"""Public Docker fakes shared by backend composition tests."""

from __future__ import annotations

from typing import Protocol

from vs_sandbox.api.testing import ScriptedDockerCli, docker_result


class _StartableSandbox(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


def recording_docker_cli() -> ScriptedDockerCli:
    """Return a public Docker fake configured for sandbox lifecycle calls."""
    docker = ScriptedDockerCli()
    docker.on(("docker", "run"), docker_result(stdout="test-container-id\n"))
    docker.always(docker_result())
    return docker


def capture_docker_start_argv(
    sandbox: _StartableSandbox,
    docker: ScriptedDockerCli,
) -> list[str]:
    """Return the Docker argv issued by a sandbox's public start operation."""
    try:
        sandbox.start()
    finally:
        sandbox.stop()
    return list(next(argv for argv in docker.argvs if argv[1:2] == ("run",)))
