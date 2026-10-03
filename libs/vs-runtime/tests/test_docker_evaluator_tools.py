"""Public contract for target-image evaluator tool preparation."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    prepare_docker_evaluator_resources,
)
from vs_sandbox.api import (
    HostResource,
    HostResourceAccess,
    SandboxExecutionResult,
    SandboxKind,
)
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
)
from vs_sandbox.api.testing import (
    DEFAULT_RESULT,
    FakeComputeBackend,
    FakeLifecycleSandbox,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_IMAGE = "sha256:target-image"


def _tool() -> CargoGitToolSpec:
    return CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools/check.git",
        rev="a" * 40,
        package="check-tool",
        bins=("check",),
    )


def _requirements(tmp_path: Path) -> TrustedEvaluatorRequirements:
    return TrustedEvaluatorRequirements(
        package_root=tmp_path / "evaluator-package",
        tools={"check-tool": _tool()},
        tools_root=tmp_path / "operator-tools",
    )


def _builder_workspace(log_dir: Path) -> Path:
    return log_dir / "evaluator-tool-builder-workspace"


def _install_fake_binary(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    install_root = Path(arguments[arguments.index("--root") + 1])
    binary = install_root / "bin" / "check"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return subprocess.CompletedProcess(arguments, 0, "", "")


def test_verified_cache_returns_read_only_resources_without_a_builder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    host_parent = docker_evaluator_tools_root(
        requirements,
        workspace,
        image_identity=_IMAGE,
    )
    prepare_evaluator_tools(
        requirements.tools,
        host_parent,
        command_runner=_install_fake_binary,
    )
    backend = FakeComputeBackend()

    resources = prepare_docker_evaluator_resources(
        requirements,
        workspace,
        backend=backend,
        log_dir=log_dir,
        container_image=_IMAGE,
    )

    spec = requirements.tools["check-tool"]
    assert resources == (
        HostResource(
            tool_install_root(host_parent, "check-tool", spec),
            HostResourceAccess.READ_ONLY,
            "evaluator tool",
            str(tool_install_root(SANDBOX_EVALUATOR_TOOLS_ROOT, "check-tool", spec)),
        ),
    )
    assert backend.creations == []
    assert not log_dir.exists()


def test_empty_requirements_do_not_require_cache_or_builder_paths(tmp_path: Path) -> None:
    backend = FakeComputeBackend()

    assert (
        prepare_docker_evaluator_resources(
            TrustedEvaluatorRequirements(),
            tmp_path / "missing-workspace",
            backend=backend,
            log_dir=tmp_path / "missing-logs",
            container_image=_IMAGE,
        )
        == ()
    )
    assert backend.creations == []


def test_declared_tools_require_an_operator_owned_cache_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = TrustedEvaluatorRequirements(
        package_root=tmp_path / "evaluator-package",
        tools={"check-tool": _tool()},
    )

    with pytest.raises(ValueError, match="require an operator-owned tools root"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            backend=FakeComputeBackend(),
            log_dir=tmp_path / "logs",
            container_image=_IMAGE,
        )


def test_missing_cache_uses_target_image_ephemeral_builder_then_verifies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    backend = FakeComputeBackend()
    sandbox = FakeLifecycleSandbox()
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            backend=backend,
            log_dir=log_dir,
            container_image=_IMAGE,
        )

    assert len(backend.creations) == 1
    creation = backend.creations[0]
    assert creation.kind is SandboxKind.DOCKER
    assert creation.host_workspace == str(_builder_workspace(log_dir))
    assert creation.log_path == log_dir / "evaluator-tool-builder.log"
    assert creation.attach_accelerator is False
    assert creation.ephemeral is True
    assert creation.container_image == _IMAGE
    assert creation.bind_mounts[0][1:] == (str(SANDBOX_EVALUATOR_TOOLS_ROOT), False)
    assert len(creation.lifecycle_hooks) == 1
    assert isinstance(creation.lifecycle_hooks[0], EvaluatorToolLifecycleHooks)
    assert any(
        "static.rust-lang.org/rustup/dist" in command for command in creation.extra_init_commands
    )
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1
    ownership = sandbox.calls[-1]
    ownership_arguments = shlex.split(ownership.command)
    assert ownership_arguments[:2] == ["sh", "-c"]
    assert "stat -c" in ownership_arguments[2]
    assert "chown -R" in ownership_arguments[2]
    assert ".host-owner-" in ownership_arguments[4]
    assert ownership.timeout == 120


def test_builder_ownership_failure_is_bounded_and_still_stops(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    backend = FakeComputeBackend()
    sandbox = FakeLifecycleSandbox(
        default_result=SandboxExecutionResult(output="denied\n" + "x" * 800, exit_code=1)
    )
    sandbox.script(
        evaluator_tools_install_command(requirements.tools, SANDBOX_EVALUATOR_TOOLS_ROOT),
        DEFAULT_RESULT,
    )
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError) as raised:
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            backend=backend,
            log_dir=log_dir,
            container_image=_IMAGE,
        )

    assert "could not return cache ownership" in str(raised.value)
    assert "denied" in str(raised.value)
    assert len(str(raised.value).partition(": ")[2]) == 500
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1


def test_builder_start_failure_still_attempts_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    backend = FakeComputeBackend()
    sandbox = FakeLifecycleSandbox(start_error=RuntimeError("container failed to start"))
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(RuntimeError, match="container failed to start"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            backend=backend,
            log_dir=log_dir,
            container_image=_IMAGE,
        )

    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1
