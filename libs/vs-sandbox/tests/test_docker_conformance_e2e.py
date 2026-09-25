"""Docker's own case of the shared-resource-list conformance contract.

The container is the confinement, not a namespace tool, so this needs a real
Docker build and a real container instead of the skip-when-unavailable
backends in ``test_sandbox_conformance.py``. Skipped unless
``VIBESYS_E2E_DOCKER=1`` and ``docker`` is on PATH, so an ordinary ``pytest``
run needs neither Docker nor the image build time:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest libs/vs-sandbox/tests/test_docker_conformance_e2e.py -q -s
```
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.support import run_test_command

from vibesys.sandbox.images import (
    agent_image,
)
from vs_sandbox.docker_sandbox import (
    DockerSandbox,
)
from vs_sandbox.host_resources import HostResource, HostResourceAccess

pytestmark = pytest.mark.e2e

_ENABLE_ENV = "VIBESYS_E2E_DOCKER"

#: Base image for the Docker case: small, Debian-derived (the agent layer's
#: apt step requires it), and already used by ``tests/e2e/test_agent_image_e2e.py``
#: and the CPU backend, so it and its early layers are typically cached.
_DOCKER_BASE_IMAGE = "python:3.12-bookworm"
_DOCKER_BUILD_TIMEOUT_S = 600.0


def _enabled() -> bool:
    return os.environ.get(_ENABLE_ENV) == "1"


def _probe_script(workspace: Path, readonly_path: Path, unlisted_path: Path) -> str:
    """Shell script exercising every conformance check in one subprocess."""
    written = workspace / "written-by-probe.txt"
    return (
        f"printf ok > {written}; printf 'write_ws=%s\\n' $?; "
        f"(printf x > {readonly_path}) 2>/dev/null; printf 'write_ro=%s\\n' $?; "
        f"cat {unlisted_path} >/dev/null 2>&1; printf 'read_unlisted=%s\\n' $?; "
        "printf 'uid=%s\\n' \"$(id -u)\"; "
        "printf 'HOME=%s\\n' \"$HOME\"; "
        "printf 'PATH=%s\\n' \"$PATH\""
    )


def _parse_probe_output(stdout: str) -> dict[str, str]:
    """Parse the ``name=value`` lines :func:`_probe_script` prints, one per check."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            fields[name] = value
    return fields


@pytest.mark.skipif(
    not _enabled() or shutil.which("docker") is None,
    reason=f"set {_ENABLE_ENV}=1 with docker on PATH to build and run the agent image",
)
def test_docker_probe_enforces_the_shared_resource_contract(tmp_path: Path) -> None:
    """Docker's own case: the container is the confinement, not a namespace tool.

    Builds the real CPU agent image (cached by Docker's layer cache after the
    first run), starts a real container from a resource list, and drives the
    same checks as :class:`~test_sandbox_conformance.TestSandboxConformance`
    through :meth:`~vs_sandbox.docker_sandbox.DockerSandbox.wrap` — but manages
    its own container lifecycle explicitly, since that shared harness has no
    per-backend teardown and a container must be stopped and removed however
    the test ends.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    readonly_path = tmp_path / "readonly" / "config.json"
    readonly_path.parent.mkdir()
    readonly_path.write_text('{"k": "v"}\n')
    unlisted_path = tmp_path / "unlisted" / "secret.txt"
    unlisted_path.parent.mkdir()
    unlisted_path.write_text("do not read\n")
    resources = (HostResource(readonly_path, HostResourceAccess.READ_ONLY, "conformance fixture"),)

    image = agent_image(_DOCKER_BASE_IMAGE, timeout=_DOCKER_BUILD_TIMEOUT_S)
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=image,
        resources=resources,
    )
    sandbox.start()
    try:
        script = _probe_script(
            Path(sandbox.agent_path(workspace)),
            Path(sandbox.agent_path(readonly_path)),
            unlisted_path,
        )
        result = run_test_command(
            sandbox.wrap(["/bin/sh", "-c", script], workspace),
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        fields = _parse_probe_output(result.stdout)

        # Write inside the workspace succeeds.
        assert fields["write_ws"] == "0"
        assert (workspace / "written-by-probe.txt").read_text() == "ok"
        # Write to the declared read-only resource fails, and nothing changed.
        assert fields["write_ro"] != "0"
        assert readonly_path.read_text() == '{"k": "v"}\n'
        # The unlisted path was never mounted, so it is simply absent.
        assert fields["read_unlisted"] != "0"
        # The agent user is remapped to the host uid at start(): no privilege change.
        assert fields["uid"] == str(os.getuid())
        # HOME and PATH inside match what the sandbox promises through env.
        assert fields["HOME"] == sandbox.env["HOME"]
        assert fields["PATH"] == sandbox.env["PATH"]
    finally:
        sandbox.stop()
