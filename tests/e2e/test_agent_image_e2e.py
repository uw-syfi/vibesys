"""Builds a real agent image and runs every shipped CLI inside it.

Skipped unless ``VIBESYS_E2E_DOCKER=1`` and ``docker`` is on PATH, so an
ordinary ``pytest`` run needs neither Docker nor network access:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_agent_image_e2e.py -q -s
```

Building the CPU variant (``python:3.12-bookworm`` base, both optional
toolchains) needs network access to fetch Node, the four CLI packages,
rustup, and the Go toolchain, and can take several minutes even on a fast
connection. A rebuild with nothing changed is fast: Docker's layer cache
answers it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from vibesys.sandbox.images import agent_image

ENABLE_ENV = "VIBESYS_E2E_DOCKER"

pytestmark = pytest.mark.e2e

#: Generous: fetching Node, four npm-installed CLIs, rustup, and the Go
#: toolchain over a real network, from a cold Docker layer cache.
_BUILD_TIMEOUT_S = 900.0
_RUN_TIMEOUT_S = 120.0

#: Proves the image runs as the non-root ``agent`` user with the fixed uid
#: the sandbox remaps to the host uid at container start (see
#: ``agent.Dockerfile``), then that every shipped CLI, both optional
#: toolchains, ripgrep, and the ``mcp`` package are all reachable.
_CHECK_SCRIPT = (
    "set -e; "
    "id -u; "
    "claude --version; "
    "codex --version; "
    "gemini --version; "
    "opencode --version; "
    "cargo --version; "
    "go version; "
    "rg --version; "
    'python3 -c "import mcp"'
)


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


@pytest.mark.skipif(
    not _enabled() or shutil.which("docker") is None,
    reason=f"set {ENABLE_ENV}=1 with docker on PATH to build and run the agent image",
)
def test_cpu_agent_image_runs_every_shipped_cli_as_the_agent_user() -> None:
    image_id = agent_image(
        "python:3.12-bookworm",
        toolchains=("rust", "go"),
        timeout=_BUILD_TIMEOUT_S,
    )

    result = subprocess.run(  # noqa: S603
        ("docker", "run", "--rm", image_id, "bash", "-lc", _CHECK_SCRIPT),  # noqa: S607
        capture_output=True,
        check=False,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )

    print(result.stdout)  # noqa: T201  # -s surfaces this for a human reading the e2e run
    print(result.stderr)  # noqa: T201
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert output_lines[0] == "1000", output_lines
