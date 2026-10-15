"""Installs every bundled evaluator tool inside a real agent image, on each base family.

Evaluator tools are built with Cargo inside the container image the run uses, so
what the image provides decides whether a tool builds. This test builds the agent
image on a Debian base and on an Ubuntu base (the ROCm PyTorch image is Ubuntu),
runs the same root setup and target-side installer a run's tool builder runs, and
checks that every declared binary exists. It needs the network (Rust, the tool's
Git repository and crates) and takes several minutes per base.

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_evaluator_tool_image_e2e.py -q -s
```
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest
from tests.support import run_test_command

from vs_agent.api.images import agent_image
from vs_runtime.api.infrastructure import (
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluatorRequirements,
    evaluator_agent_toolchains,
    evaluator_container_setup,
    image_features,
    load_evaluator_package,
)
from vs_sandbox.api.evaluator_tools import evaluator_tools_install_command, tool_install_root
from vs_sim.api import DISCARD, MonotonicClock

pytestmark = pytest.mark.e2e

_BUNDLED = sorted(
    path.parent
    for path in (Path(__file__).parents[2] / "resources" / "evaluators").glob(
        "*/vibesys.evaluator.toml"
    )
)
_BUILD_TIMEOUT_S = 900.0
_INSTALL_TIMEOUT_S = 1800.0


@pytest.mark.skipif(
    os.environ.get("VIBESYS_E2E_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set VIBESYS_E2E_DOCKER=1 with docker on PATH to build agent images",
)
@pytest.mark.parametrize("base", ["python:3.12-bookworm", "ubuntu:22.04"])
@pytest.mark.parametrize("root", _BUNDLED, ids=lambda root: root.name)
def test_every_bundled_evaluator_tool_installs_in_the_agent_image(base: str, root: Path) -> None:
    package = load_evaluator_package(root)
    if not package.metadata.tools:
        pytest.skip("evaluator declares no external tools")
    requirements = TrustedEvaluatorRequirements(
        package_root=package.root,
        toolchains=frozenset(package.metadata.toolchains),
        tools=package.metadata.tools,
        tools_root=root / "tools",
    )
    image_id = agent_image(
        base,
        features=image_features(evaluator_agent_toolchains(requirements)),
        timeout=_BUILD_TIMEOUT_S,
        clock=MonotonicClock(),
        output=DISCARD,
    )
    script = "set -eux\n" + "\n".join(
        (
            *evaluator_container_setup(requirements),
            evaluator_tools_install_command(requirements.tools, SANDBOX_EVALUATOR_TOOLS_ROOT),
            *(
                shlex.join(
                    (
                        "test",
                        "-x",
                        str(
                            tool_install_root(SANDBOX_EVALUATOR_TOOLS_ROOT, name, spec)
                            / "bin"
                            / binary
                        ),
                    )
                )
                for name, spec in requirements.tools.items()
                for binary in spec.bins
            ),
        )
    )

    result = run_test_command(
        ("docker", "run", "--rm", "--user", "root", image_id, "sh", "-c", script),
        capture_output=True,
        check=False,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
    )

    assert result.returncode == 0, (result.returncode, result.stdout[-2000:], result.stderr[-4000:])
