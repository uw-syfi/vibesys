"""Public composition contract for trusted evaluator preparation."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    REMOTE_EVALUATOR_TOOLS_ROOT,
    ProtocolBenchmarkContract,
    TrustedEvaluationCommandPaths,
    TrustedEvaluationPlan,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_agent_toolchains,
    evaluator_container_setup,
    prepare_trusted_evaluation_plan,
    remote_evaluator_setup_command,
    required_evaluator_tools_root,
)
from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN, PYTHON_TOKEN
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec, tool_path_replacements

if TYPE_CHECKING:
    from subprocess import CompletedProcess


def _tool() -> CargoGitToolSpec:
    return CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools/check.git",
        rev="a" * 40,
        package="check-tool",
        bins=("check",),
    )


def _requirements(tmp_path: Path, *, tools: bool = False) -> TrustedEvaluatorRequirements:
    return TrustedEvaluatorRequirements(
        package_root=tmp_path / "evaluator-package",
        toolchains=frozenset({"go"}),
        tools={"check-tool": _tool()} if tools else {},
        tools_root=tmp_path / "operator-tools",
    )


def test_authored_command_tokens_remain_stable() -> None:
    assert PROJECT_ROOT_TOKEN == "$" + "{PROJECT_ROOT}"
    assert PYTHON_TOKEN == "$" + "{PYTHON}"


def test_preparation_lowers_commands_without_replacing_the_plan_contract(tmp_path: Path) -> None:
    requirements = _requirements(tmp_path, tools=True)
    package_root = tmp_path / "evaluator-package"
    source_project = tmp_path / "workspace"
    tool_token = next(iter(tool_path_replacements(requirements.tools, Path("/tools"))))
    plan = TrustedEvaluationPlan(
        accuracy_command=shlex.join(
            (PYTHON_TOKEN, str(package_root / "accuracy.py"), PROJECT_ROOT_TOKEN)
        ),
        benchmark_command=shlex.join((tool_token, "--project", PROJECT_ROOT_TOKEN)),
        benchmark_timeout_seconds=19,
        benchmark_contract=ProtocolBenchmarkContract(),
    )

    prepared = prepare_trusted_evaluation_plan(
        plan,
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=source_project,
            runtime_project_root="/workspace",
            python_executable="python3",
            runtime_package_root="/opt/evaluator",
            runtime_tools_root=Path("/opt/tools"),
        ),
    )

    assert shlex.split(prepared.accuracy_command or "") == [
        "python3",
        "/opt/evaluator/accuracy.py",
        "/workspace",
    ]
    assert shlex.split(prepared.benchmark_command or "") == [
        next(iter(tool_path_replacements(requirements.tools, Path("/opt/tools")).values())),
        "--project",
        "/workspace",
    ]
    assert prepared.benchmark_timeout_seconds == 19
    assert prepared.benchmark_contract == ProtocolBenchmarkContract()
    assert plan.accuracy_command != prepared.accuracy_command


def test_preparation_rejects_invalid_shell_and_missing_runtime_package_root(
    tmp_path: Path,
) -> None:
    requirements = _requirements(tmp_path)
    package_root = tmp_path / "evaluator-package"
    paths = TrustedEvaluationCommandPaths(
        source_project_root=tmp_path / "workspace",
        runtime_project_root="/workspace",
        python_executable="python3",
    )

    with pytest.raises(ValueError, match="invalid evaluator command"):
        prepare_trusted_evaluation_plan(
            TrustedEvaluationPlan(accuracy_command="python 'unterminated"),
            requirements,
            paths,
        )
    with pytest.raises(ValueError, match="runtime package root"):
        prepare_trusted_evaluation_plan(
            TrustedEvaluationPlan(accuracy_command=str(package_root / "check.py")),
            requirements,
            paths,
        )


def test_tools_root_is_operator_owned_and_docker_cache_is_image_specific(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = _requirements(tmp_path)

    assert (
        required_evaluator_tools_root(requirements, workspace)
        == (tmp_path / "operator-tools").resolve()
    )
    first = docker_evaluator_tools_root(
        requirements,
        workspace,
        image_identity="sha256:first",
    )
    second = docker_evaluator_tools_root(
        requirements,
        workspace,
        image_identity="sha256:second",
    )
    assert first.parent == (tmp_path / "operator-tools" / "docker").resolve()
    assert first != second

    inside = TrustedEvaluatorRequirements(
        package_root=tmp_path / "package",
        tools_root=workspace / "cache",
    )
    with pytest.raises(ValueError, match="outside the candidate workspace"):
        required_evaluator_tools_root(inside, workspace)


def test_tool_setup_is_derived_from_declared_requirements(tmp_path: Path) -> None:
    requirements = _requirements(tmp_path, tools=True)

    assert evaluator_agent_toolchains(requirements) == frozenset({"go", "rust"})
    setup = remote_evaluator_setup_command(requirements)
    assert setup is not None
    assert str(REMOTE_EVALUATOR_TOOLS_ROOT) in setup
    assert "cargo install" in setup
    assert remote_evaluator_setup_command(TrustedEvaluatorRequirements()) is None


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _run_rootless_rust_setup(
    tmp_path: Path,
    *,
    downloader_exit_code: int = 0,
) -> CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "rustc",
        "#!/bin/sh\nprintf '%s\\n' 'rustc 1.85.0 (fake)'\n",
    )
    _write_executable(
        fake_bin / "cargo",
        "#!/bin/sh\necho 'rustup has no configured default toolchain' >&2\nexit 1\n",
    )

    rustup_init = tmp_path / "fake-rustup-init"
    working_cargo = tmp_path / "working-cargo"
    working_rustc = tmp_path / "working-rustc"
    _write_executable(
        working_cargo,
        "#!/bin/sh\nprintf '%s\\n' 'cargo 1.92.0 (fake)'\n",
    )
    _write_executable(
        working_rustc,
        "#!/bin/sh\nprintf '%s\\n' 'rustc 1.92.0 (fake)'\n",
    )
    _write_executable(
        rustup_init,
        "#!/bin/sh\n"
        'mkdir -p "$CARGO_HOME/bin" "$RUSTUP_HOME"\n'
        'cp "$FAKE_WORKING_CARGO" "$CARGO_HOME/bin/cargo"\n'
        'cp "$FAKE_WORKING_RUSTC" "$CARGO_HOME/bin/rustc"\n'
        'chmod +x "$CARGO_HOME/bin/cargo" "$CARGO_HOME/bin/rustc"\n',
    )
    downloader = (
        '#!/bin/sh\ncp "$FAKE_RUSTUP_INIT" "$4"\n'
        if downloader_exit_code == 0
        else f"#!/bin/sh\nexit {downloader_exit_code}\n"
    )
    _write_executable(fake_bin / "python3", downloader)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands = evaluator_container_setup(
        TrustedEvaluatorRequirements(
            package_root=tmp_path / "evaluator-package",
            toolchains=frozenset({"rust"}),
        ),
        rootless=True,
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RUSTUP_INIT": str(rustup_init),
        "FAKE_WORKING_CARGO": str(working_cargo),
        "FAKE_WORKING_RUSTC": str(working_rustc),
    }
    # lint-waiver: LW-837219 [S603]; this fixed local shell argv exercises the
    # generated setup contract, while using the application's test-process
    # wrapper would couple a lower-library test upward to VibeSys imports.
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", "set -e\n" + "\n".join((*commands, "cargo --version"))],
        cwd=workspace,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def test_rootless_rust_setup_replaces_broken_rustup_cargo_shim(tmp_path: Path) -> None:
    result = _run_rootless_rust_setup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "cargo 1.92.0 (fake)" in result.stdout
    assert (tmp_path / "workspace" / ".bin" / "cargo").is_symlink()


def test_rootless_rust_setup_does_not_mask_download_failure(tmp_path: Path) -> None:
    result = _run_rootless_rust_setup(tmp_path, downloader_exit_code=7)

    assert result.returncode != 0
    assert "failed to download evaluator Rust toolchain" in result.stderr
    assert "cargo 1.92.0 (fake)" not in result.stdout
