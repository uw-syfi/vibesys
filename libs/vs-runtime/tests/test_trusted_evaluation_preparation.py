"""Public composition contract for trusted evaluator preparation."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    REMOTE_EVALUATOR_GO_MODULES_ROOT,
    REMOTE_EVALUATOR_TOOLS_ROOT,
    EvaluatorGoModule,
    EvaluatorPackageError,
    PreparedTrustedEvaluator,
    ProtocolBenchmarkContract,
    TrustedEvaluationCommandPaths,
    TrustedEvaluationPlan,
    TrustedEvaluatorRequirements,
    docker_evaluator_tools_root,
    evaluator_agent_toolchains,
    evaluator_container_setup,
    evaluator_go_module_ready_path,
    evaluator_go_module_setup_command,
    prepare_trusted_evaluation_plan,
    prepare_trusted_evaluator,
    remote_evaluator_setup_command,
    required_evaluator_tools_root,
)
from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN, PYTHON_TOKEN
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    tool_install_root,
    tool_path_replacements,
)

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


def _write_evaluator_package(root: Path, *, with_tool: bool) -> Path:
    root.mkdir()
    tool_metadata = (
        """
[tools.check-tool]
kind = "cargo-git"
git = "https://example.com/tools/check.git"
rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
package = "check-tool"
bins = ["check"]
"""
        if with_tool
        else ""
    )
    command = '"${TOOL:check-tool/check}"' if with_tool else '"python"'
    (root / "vibesys.evaluator.toml").write_text(
        f"""schema_version = 1
name = "vibesys-evaluator-test"
version = "1.0.0"
protocol_version = 1
toolchains = ["go"]
[entrypoints]
check = [{command}]
{tool_metadata}""",
        encoding="utf-8",
    )
    return root


def _write_go_evaluator_package(root: Path) -> Path:
    root.mkdir()
    (root / "go.mod").write_text("module example.com/evaluator\n\ngo 1.21\n", encoding="utf-8")
    (root / "go.sum").write_text("", encoding="utf-8")
    (root / "vibesys.evaluator.toml").write_text(
        """schema_version = 1
name = "vibesys-evaluator-test"
version = "1.0.0"
protocol_version = 1
toolchains = ["go"]
go_module = "."
[entrypoints]
check = ["go", "-C", "${PACKAGE_ROOT}", "run", "."]
""",
        encoding="utf-8",
    )
    return root


def test_trusted_evaluator_preparation_is_empty_without_a_package(tmp_path: Path) -> None:
    prepared = prepare_trusted_evaluator(None, tmp_path / "unused-cache")

    assert prepared == PreparedTrustedEvaluator(TrustedEvaluatorRequirements())


def test_trusted_evaluator_preparation_derives_requirements_and_tool_roots(
    tmp_path: Path,
) -> None:
    package_root = _write_evaluator_package(tmp_path / "package", with_tool=True)
    tools_root = tmp_path / "model-cache" / "evaluator-tools"

    prepared = prepare_trusted_evaluator(package_root, tools_root)

    assert prepared.requirements.package_root == package_root.resolve()
    assert prepared.requirements.toolchains == frozenset({"go"})
    assert tuple(prepared.requirements.tools) == ("check-tool",)
    assert prepared.requirements.tools_root == tools_root
    assert prepared.tool_roots == (
        tool_install_root(
            tools_root,
            "check-tool",
            prepared.requirements.tools["check-tool"],
        ),
    )


def test_trusted_evaluator_preparation_omits_cache_for_package_without_tools(
    tmp_path: Path,
) -> None:
    package_root = _write_evaluator_package(tmp_path / "package", with_tool=False)

    prepared = prepare_trusted_evaluator(package_root, tmp_path / "unused-cache")

    assert prepared.requirements.package_root == package_root.resolve()
    assert prepared.requirements.toolchains == frozenset({"go"})
    assert prepared.requirements.tools == {}
    assert prepared.requirements.tools_root is None
    assert prepared.tool_roots == ()


def test_trusted_evaluator_preparation_keys_go_cache_by_immutable_package(
    tmp_path: Path,
) -> None:
    package_root = _write_go_evaluator_package(tmp_path / "package")
    cache_root = tmp_path / "operator-cache"

    prepared = prepare_trusted_evaluator(package_root, cache_root)

    module = prepared.requirements.go_module
    assert module is not None
    assert module.root == package_root
    assert module.package_digest.startswith("sha256:")
    assert module.cache_root == (
        cache_root / "go-modules" / module.package_digest.removeprefix("sha256:")
    )
    assert prepared.requirements.tools_root is None
    assert prepared.tool_roots == (module.cache_root,)


def test_trusted_evaluator_preparation_preserves_package_validation_errors(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(
        EvaluatorPackageError,
        match=f"evaluator package is not a directory: {missing}",
    ):
        prepare_trusted_evaluator(missing, tmp_path / "model-cache")


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


def test_go_evaluator_commands_are_forced_onto_the_prepared_offline_cache(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "evaluator-package"
    module_root = package_root / "module"
    cache_root = tmp_path / "operator-cache"
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=EvaluatorGoModule(
            root=module_root,
            cache_root=cache_root,
            package_digest="sha256:" + "a" * 64,
        ),
    )

    prepared = prepare_trusted_evaluation_plan(
        TrustedEvaluationPlan(
            accuracy_command=shlex.join(("python3", str(package_root / "wrapper.py")))
        ),
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=tmp_path / "workspace",
            runtime_project_root="/workspace",
            python_executable="python3",
            runtime_package_root="/opt/evaluator",
            runtime_go_module_cache_root="/opt/go-modules",
        ),
    )

    assert shlex.split(prepared.accuracy_command or "") == [
        "env",
        "GOMODCACHE=/opt/go-modules",
        "GOPROXY=off",
        "GOSUMDB=off",
        "GOWORK=off",
        "GOENV=off",
        "GOTOOLCHAIN=local",
        "GOFLAGS=-mod=readonly -buildvcs=false",
        "python3",
        "/opt/evaluator/wrapper.py",
    ]


def test_relative_remote_go_cache_is_resolved_before_exec(tmp_path: Path) -> None:
    package_root = tmp_path / "evaluator-package"
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=EvaluatorGoModule(
            root=package_root,
            cache_root=tmp_path / "operator-cache",
            package_digest="sha256:" + "c" * 64,
        ),
    )

    prepared = prepare_trusted_evaluation_plan(
        TrustedEvaluationPlan(accuracy_command="python3 wrapper.py"),
        requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=tmp_path / "workspace",
            runtime_project_root=".",
            python_executable="python3",
            runtime_package_root=".vibesys-evaluator-package",
            runtime_go_module_cache_root=str(REMOTE_EVALUATOR_GO_MODULES_ROOT),
        ),
    )

    command = shlex.split(prepared.accuracy_command or "")
    assert command[:2] == ["sh", "-c"]
    assert 'exec env "GOMODCACHE=$cache_root"' in command[2]
    assert command[4:] == [
        str(REMOTE_EVALUATOR_GO_MODULES_ROOT),
        "python3",
        "wrapper.py",
    ]


def test_go_module_setup_downloads_before_an_explicit_offline_verification(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "evaluator-package"
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=EvaluatorGoModule(
            root=package_root / "module",
            cache_root=tmp_path / "operator-cache",
            package_digest="sha256:" + "b" * 64,
        ),
    )

    command = evaluator_go_module_setup_command(
        requirements,
        runtime_package_root=".vibesys-evaluator-package",
        runtime_cache_root=str(REMOTE_EVALUATOR_GO_MODULES_ROOT),
    )

    assert command.index("mod download all") < command.index("GOPROXY=off")
    assert "GOFLAGS='-mod=readonly -buildvcs=false'" in command
    assert "mod verify" in command
    assert '["go", "-C", module_root, "list", "-deps", "./..."]' in command
    assert "if process is not None:\n        begin_interrupt()" in command
    assert command.index("process = subprocess.Popen") < command.index(
        "if interrupted is not None:\n        begin_interrupt()"
    )
    assert (
        "wait_without_reaping(process)\n"
        "    # From here onward every path is cleanup. A first late signal may record\n"
        "    # interruption, but must not raise from inside the finally block.\n"
        "    unwinding = True\n"
        "except InterruptedError:"
    ) in command
    assert (
        "unwinding = True\n"
        "    # Make cleanup one-shot before control leaves the interrupted operation.\n"
        "    # A second handled signal must never raise from inside the finally block.\n"
        "    signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)\n"
        "    for handled in handled_signals:\n"
        "        signal.signal(handled, signal.SIG_IGN)\n"
        "    raise InterruptedError"
    ) in command
    assert "os.WEXITED | os.WNOWAIT" in command
    assert "start_new_session=True" in command
    assert "os.killpg(process.pid, signal.SIGTERM)" in command
    assert "os.killpg(process.pid, signal.SIGKILL)" in command
    assert command.index("os.killpg(process.pid, signal.SIGKILL)") < command.index(
        "leader_status = process.wait()"
    )
    assert "failed to prepare evaluator Go modules" in command
    assert "vibesys-evaluator-go-verify-" in command
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in command
    assert "finally:" in command
    assert ".verification-build." not in command
    assert "$ready.tmp." not in command
    assert "$$" not in command


def test_go_module_readiness_marker_is_an_atomic_digest_named_directory(tmp_path: Path) -> None:
    module = EvaluatorGoModule(
        root=tmp_path / "package",
        cache_root=tmp_path / "cache",
        package_digest="sha256:" + "d" * 64,
    )

    assert evaluator_go_module_ready_path(module, module.cache_root) == (
        module.cache_root / f".vibesys-ready.{module.package_digest}"
    )


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


def test_rootless_setup_never_runs_the_package_manager_for_declared_packages(
    tmp_path: Path,
) -> None:
    tool = _tool().model_copy(update={"system_packages": ("libssl-dev",)})
    commands = evaluator_container_setup(
        TrustedEvaluatorRequirements(
            package_root=tmp_path / "evaluator-package",
            tools={"check-tool": tool},
            tools_root=tmp_path / "tools",
        ),
        rootless=True,
    )

    assert not any("apt-get" in command for command in commands)
