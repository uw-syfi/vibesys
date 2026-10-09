"""A container-topology task's Docker sandbox gets its own Docker daemon under Sysbox.

Starts the sandbox the hotel-reservation task would get (the production
container-runtime policy, a real agent image carrying the container-runtime
toolchain, the real Sysbox runtime) and drives the daemon inside it the way the
agent and the evaluator do. Skipped unless ``VIBESYS_E2E_SYSBOX=1``, ``docker``
is on PATH, and the host daemon registers ``sysbox-runc``:

```bash
VIBESYS_E2E_SYSBOX=1 uv run pytest tests/e2e/test_sysbox_topology_e2e.py -q -s
```

The first run builds the agent image (Node, four CLIs, Docker engine, compose,
kind, kubectl) and pulls ``alpine`` into the sandbox's own daemon, so it needs
network access; Docker's layer cache answers a repeat build.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_agent.api import CONTAINER_RUNTIME_TOOLCHAIN
from vs_agent.api.images import agent_image
from vs_sandbox.api import AGENT_HOME, DockerSandbox

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

pytestmark = pytest.mark.e2e

_ENABLE_ENV = "VIBESYS_E2E_SYSBOX"
_BASE_IMAGE = "debian:bookworm"
_BUILD_TIMEOUT_S = 1800.0
_EXEC_TIMEOUT_S = 300
#: Compose names its project after the workspace directory.
_TASK_PROJECT = "workspace-probe"


def _sysbox_registered() -> bool:
    if shutil.which("docker") is None:
        return False
    info = run_test_command(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        text=True,
        capture_output=True,
        timeout=60,
    )
    return info.returncode == 0 and "sysbox-runc" in json.loads(info.stdout or "{}")


_skip_reason = (
    f"set {_ENABLE_ENV}=1 on a host with docker and the sysbox-runc runtime to run this test"
)


@pytest.mark.skipif(
    os.environ.get(_ENABLE_ENV) != "1" or not _sysbox_registered(), reason=_skip_reason
)
def test_hotel_reservation_sandbox_runs_compose_and_containers_on_its_own_daemon(
    tmp_path: Path,
) -> None:
    capture = f"{AGENT_HOME}/e2e-capture"
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "seed.txt").write_text("from-the-workspace\n", encoding="utf-8")
    # A compose file shaped like the hotel-reservation one: a relative bind
    # source (the workspace) and an absolute one (a directory in the sandbox),
    # both resolved by the daemon inside the sandbox.
    (workspace / "docker-compose.yml").write_text(
        f"""services:
  probe:
    image: alpine
    command: ["sh", "-c", "cat /seed/seed.txt /capture/capture.txt"]
    volumes:
      - ./data:/seed:ro
      - {capture}:/capture:ro
""",
        encoding="utf-8",
    )
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=agent_image(
            _BASE_IMAGE, toolchains={CONTAINER_RUNTIME_TOOLCHAIN}, timeout=_BUILD_TIMEOUT_S
        ),
        docker_in_docker=True,
    )

    def inside(command: str) -> subprocess.CompletedProcess[str]:
        return run_test_command(
            sandbox.wrap(["bash", "-c", command], workspace),
            text=True,
            capture_output=True,
            timeout=_EXEC_TIMEOUT_S,
        )

    sandbox.start()
    try:
        assert (
            inside(
                f"mkdir -p {capture} && echo from-the-sandbox > {capture}/capture.txt"
            ).returncode
            == 0
        )
        assert inside("docker compose config --quiet").returncode == 0
        assert inside("docker run --rm alpine true").returncode == 0
        probe = inside("docker compose run --rm probe")
        assert probe.returncode == 0, probe.stderr
        assert "from-the-workspace" in probe.stdout
        assert "from-the-sandbox" in probe.stdout
        # The topology ran on the sandbox's own daemon, not the host's: it pulled
        # alpine itself, the host daemon has no compose container, and the host
        # socket is not a mount inside the sandbox.
        assert inside("docker image inspect alpine").returncode == 0
        assert _host_container_names(_TASK_PROJECT) == ""
        assert inside("! grep -q docker.sock /proc/self/mountinfo").returncode == 0
    finally:
        sandbox.stop()


def _host_container_names(project: str) -> str:
    listing = run_test_command(
        ["docker", "ps", "-a", "--filter", f"name={project}", "--format", "{{.Names}}"],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return listing.stdout.strip()
