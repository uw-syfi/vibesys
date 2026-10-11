"""Every bundled evaluator tool's declared system packages reach a Docker target's setup."""

from __future__ import annotations

from pathlib import Path

import pytest

from vs_runtime.api.infrastructure import (
    TrustedEvaluatorRequirements,
    evaluator_container_setup,
    load_evaluator_package,
)

_BUNDLED = sorted(
    path.parent
    for path in (Path(__file__).parents[3] / "resources" / "evaluators").glob(
        "*/vibesys.evaluator.toml"
    )
)


@pytest.mark.parametrize("root", _BUNDLED, ids=lambda root: root.name)
def test_container_setup_installs_every_system_package_a_bundled_tool_declares(
    root: Path,
) -> None:
    package = load_evaluator_package(root)
    requirements = TrustedEvaluatorRequirements(
        package_root=package.root,
        toolchains=frozenset(package.metadata.toolchains),
        tools=package.metadata.tools,
        tools_root=root / "tools",
    )
    declared = {name for spec in package.metadata.tools.values() for name in spec.system_packages}

    setup = "\n".join(evaluator_container_setup(requirements))

    for name in declared:
        assert "apt-get install" in setup
        assert name in setup
