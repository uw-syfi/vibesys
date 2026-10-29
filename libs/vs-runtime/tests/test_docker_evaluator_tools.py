"""Public contract for target-image evaluator tool preparation."""

from __future__ import annotations

import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    SANDBOX_EVALUATOR_GO_MODULES_ROOT,
    SANDBOX_EVALUATOR_TOOLS_ROOT,
    DockerToolBuilder,
    EvaluatorGoModule,
    EvaluatorGoModuleError,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_container_setup,
    evaluator_go_module_ready_path,
    evaluator_go_module_setup_command,
    prepare_docker_evaluator_resources,
)
from vs_sandbox.api import (
    CommandResult,
    HostResource,
    HostResourceAccess,
    SandboxKind,
)
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    EvaluatorToolError,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
)
from vs_sandbox.api.testing import (
    DEFAULT_RESULT,
    FakeComputeBackend,
    FakeLifecycleRunner,
)
from vs_sim.api import Capture
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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


def _go_requirements(tmp_path: Path) -> TrustedEvaluatorRequirements:
    package_root = tmp_path / "evaluator-package"
    package_root.mkdir()
    return TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=EvaluatorGoModule(
            root=package_root,
            cache_root=tmp_path / "operator-go-modules" / ("a" * 64),
            package_digest="sha256:" + "a" * 64,
        ),
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
    backend = FakeComputeBackend(threads=SimThreads())

    resources = prepare_docker_evaluator_resources(
        requirements,
        workspace,
        builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
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
    backend = FakeComputeBackend(threads=SimThreads())

    assert (
        prepare_docker_evaluator_resources(
            TrustedEvaluatorRequirements(),
            tmp_path / "missing-workspace",
            builder=DockerToolBuilder(
                backend=backend, log_dir=tmp_path / "missing-logs", image=_IMAGE
            ),
        )
        == ()
    )
    assert backend.creations == []


def test_verified_go_module_cache_is_reverified_in_target_image_then_mounted_read_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = _go_requirements(tmp_path)
    module = requirements.go_module
    assert module is not None
    module.cache_root.mkdir(parents=True)
    evaluator_go_module_ready_path(module, module.cache_root).mkdir()
    log_dir = tmp_path / "logs"
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner()
    backend.script_sandbox(
        SandboxKind.DOCKER,
        str(log_dir / "evaluator-go-module-builder-workspace"),
        sandbox,
    )

    resources = prepare_docker_evaluator_resources(
        requirements,
        workspace,
        builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
    )

    assert resources == (
        HostResource(
            module.cache_root,
            HostResourceAccess.READ_ONLY,
            "evaluator Go module cache",
            str(SANDBOX_EVALUATOR_GO_MODULES_ROOT),
        ),
    )
    assert len(backend.creations) == 1
    assert any(
        "mod verify" in call.command and "GOPROXY=off" in call.command for call in sandbox.calls
    )


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_matching_go_module_receipt_never_bypasses_offline_content_validation(
    tmp_path: Path,
    damage: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _go_requirements(tmp_path)
    module = requirements.go_module
    assert module is not None
    module.cache_root.mkdir(parents=True)
    evaluator_go_module_ready_path(module, module.cache_root).mkdir()
    (module.cache_root / "damaged-module.txt").write_text(damage, encoding="utf-8")
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner()
    for command in evaluator_container_setup(requirements):
        sandbox.script(command, DEFAULT_RESULT)
    validation = evaluator_go_module_setup_command(
        requirements,
        runtime_package_root="/opt/vibesys-evaluator-package",
        runtime_cache_root=str(SANDBOX_EVALUATOR_GO_MODULES_ROOT),
    )
    sandbox.script(
        validation,
        CommandResult(output=f"{damage} module content", exit_code=1),
    )
    backend.script_sandbox(
        SandboxKind.DOCKER,
        str(log_dir / "evaluator-go-module-builder-workspace"),
        sandbox,
    )

    with pytest.raises(
        EvaluatorGoModuleError,
        match=rf"target-image Go module setup failed: {damage} module content",
    ):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
        )

    assert any(call.command == validation for call in sandbox.calls)
    assert sandbox.stop_count == 1


def test_missing_go_module_cache_uses_the_target_image_and_fails_if_not_published(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _go_requirements(tmp_path)
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner()
    backend.script_sandbox(
        SandboxKind.DOCKER,
        str(log_dir / "evaluator-go-module-builder-workspace"),
        sandbox,
    )

    with pytest.raises(EvaluatorGoModuleError, match="did not publish"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
        )

    assert len(backend.creations) == 1
    creation = backend.creations[0]
    assert creation.container_image == _IMAGE
    assert creation.attach_accelerator is False
    assert creation.ephemeral is True
    assert (
        str(requirements.package_root),
        "/opt/vibesys-evaluator-package",
        True,
    ) in creation.bind_mounts
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1
    assert any("GOPROXY=off" in call.command for call in sandbox.calls)


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
            builder=DockerToolBuilder(
                backend=FakeComputeBackend(threads=SimThreads()),
                log_dir=tmp_path / "logs",
                image=_IMAGE,
            ),
        )


def test_missing_cache_uses_target_image_ephemeral_builder_then_verifies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner()
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
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
    assert creation.lifecycle_hooks == ()
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1
    ownership = sandbox.calls[-1]
    ownership_arguments = shlex.split(ownership.command)
    assert ownership_arguments[:2] == ["sh", "-c"]
    assert "stat -c" in ownership_arguments[2]
    assert "chown -R" in ownership_arguments[2]
    assert ".host-owner-" in ownership_arguments[4]
    assert ownership.timeout == 120


def _system_package_requirements(tmp_path: Path) -> TrustedEvaluatorRequirements:
    tool = _tool().model_copy(update={"system_packages": ("pkg-config", "libssl-dev")})
    return TrustedEvaluatorRequirements(
        package_root=tmp_path / "evaluator-package",
        tools={"check-tool": tool},
        tools_root=tmp_path / "operator-tools",
    )


def test_builder_installs_system_packages_as_root_before_building_tools(tmp_path: Path) -> None:
    """The tool's root setup is an exec in the started builder, not a backend parameter.

    A backend parameter was silently ignored by every real backend, so the
    packages never installed and `cargo install` failed on a real run.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _system_package_requirements(tmp_path)
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner()
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
        )

    install_tools = evaluator_tools_install_command(
        requirements.tools, SANDBOX_EVALUATOR_TOOLS_ROOT
    )
    commands = [call.command for call in sandbox.calls]
    package_step = next(
        index
        for index, call in enumerate(sandbox.calls)
        if "apt-get install" in call.command and "libssl-dev" in call.command
    )
    assert sandbox.calls[package_step].as_root
    assert package_step < commands.index(install_tools)
    assert not sandbox.calls[commands.index(install_tools)].as_root


def test_builder_setup_failure_names_the_step_and_still_stops(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _system_package_requirements(tmp_path)
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner(default_result=CommandResult(output="E: no network", exit_code=1))
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError, match="builder setup failed: E: no network"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
        )

    assert sandbox.stop_count == 1
    assert evaluator_tools_install_command(
        requirements.tools, SANDBOX_EVALUATOR_TOOLS_ROOT
    ) not in [call.command for call in sandbox.calls]


def test_builder_ownership_failure_is_bounded_and_still_stops(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    requirements = _requirements(tmp_path)
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner(
        default_result=CommandResult(output="denied\n" + "x" * 800, exit_code=1)
    )
    for command in (
        *evaluator_container_setup(requirements),
        evaluator_tools_install_command(requirements.tools, SANDBOX_EVALUATOR_TOOLS_ROOT),
    ):
        sandbox.script(command, DEFAULT_RESULT)
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(EvaluatorToolError) as raised:
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
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
    backend = FakeComputeBackend(threads=SimThreads())
    sandbox = FakeLifecycleRunner(start_error=RuntimeError("container failed to start"))
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)

    with pytest.raises(RuntimeError, match="container failed to start"):
        prepare_docker_evaluator_resources(
            requirements,
            workspace,
            builder=DockerToolBuilder(backend=backend, log_dir=log_dir, image=_IMAGE),
        )

    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1


class _BuildStreams:
    """An opener that keeps each build's output and records which builds opened and closed."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, ...]] = []
        self.closed: list[tuple[str, ...]] = []
        self.output = Capture(1 << 16)

    @contextmanager
    def open(self, tools: tuple[str, ...]) -> Iterator[Capture]:
        self.opened.append(tools)
        try:
            yield self.output
        finally:
            self.closed.append(tools)


def test_a_build_writes_its_commands_output_to_one_stream_closed_on_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    backend = FakeComputeBackend(threads=SimThreads())
    printed = "   Compiling check-tool v1.0.0\n"
    sandbox = FakeLifecycleRunner(
        default_result=CommandResult(output=printed, exit_code=0, stdout=printed)
    )
    backend.script_sandbox(SandboxKind.DOCKER, str(_builder_workspace(log_dir)), sandbox)
    streams = _BuildStreams()

    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        prepare_docker_evaluator_resources(
            _system_package_requirements(tmp_path),
            workspace,
            builder=DockerToolBuilder(
                backend=backend, log_dir=log_dir, image=_IMAGE, output=streams.open
            ),
        )

    assert streams.opened == streams.closed == [("check-tool",)]
    root_setup = [call for call in sandbox.calls if call.as_root]
    assert root_setup
    # The root setup and the installer print to the build's stream; returning the
    # cache to the host user (the last call) is the framework's and prints nowhere.
    builder_commands = len(sandbox.calls) - 1
    assert streams.output.stdout == printed.encode() * builder_commands


def test_a_verified_cache_builds_nothing_and_opens_no_stream(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = _requirements(tmp_path)
    host_parent = docker_evaluator_tools_root(requirements, workspace, image_identity=_IMAGE)
    prepare_evaluator_tools(requirements.tools, host_parent, command_runner=_install_fake_binary)
    streams = _BuildStreams()

    prepare_docker_evaluator_resources(
        requirements,
        workspace,
        builder=DockerToolBuilder(
            backend=FakeComputeBackend(threads=SimThreads()),
            log_dir=tmp_path / "logs",
            image=_IMAGE,
            output=streams.open,
        ),
    )

    assert streams.opened == []
