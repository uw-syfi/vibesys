"""Tests for immutable evaluator tool installation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibesys.evaluators import (
    CargoGitToolSpec,
    EvaluatorToolError,
    EvaluatorToolLifecycleHooks,
    cargo_install_argv,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
    tool_spec_digest,
    tool_token,
)
from vs_sandbox import BeforeReadyContext, SandboxLifecycle


def _spec() -> CargoGitToolSpec:
    return CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools",
        rev="1" * 40,
        package="example-package",
        bins=("runner", "tracegen"),
    )


def _fake_cargo(tmp_path: Path) -> tuple[Path, Path]:
    executable_dir = tmp_path / "fake-bin"
    executable_dir.mkdir()
    call_log = tmp_path / "cargo-calls"
    executable = executable_dir / "cargo"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
context_log = os.environ.get("FAKE_CARGO_CONTEXT_LOG")
if context_log:
    Path(context_log).write_text(json.dumps({{
        "cwd": os.getcwd(),
        "cargo_home": os.environ.get("CARGO_HOME"),
        "rustup_home": os.environ.get("RUSTUP_HOME"),
        "cargo_wrapper": os.environ.get("CARGO_BUILD_RUSTC_WRAPPER"),
        "rustflags": os.environ.get("RUSTFLAGS"),
        "git_config": os.environ.get("GIT_CONFIG_COUNT"),
    }}))
with Path(os.environ["FAKE_CARGO_CALL_LOG"]).open("a", encoding="utf-8") as output:
    output.write("call\\n")
stderr = os.environ.get("FAKE_CARGO_STDERR", "")
if stderr:
    print(stderr, file=sys.stderr)
exit_code = int(os.environ.get("FAKE_CARGO_EXIT", "0"))
if exit_code:
    raise SystemExit(exit_code)
root = Path(arguments[arguments.index("--root") + 1])
skip = os.environ.get("FAKE_CARGO_SKIP")
for index, argument in enumerate(arguments):
    if argument != "--bin":
        continue
    binary = arguments[index + 1]
    if binary == skip:
        continue
    path = root / "bin" / binary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("binary", encoding="utf-8")
    path.chmod(0o755)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable_dir, call_log


def _run_target_command(
    command: str,
    executable_dir: Path,
    call_log: Path,
    *,
    cwd: Path | None = None,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        shlex.split(command),
        capture_output=True,
        check=False,
        cwd=cwd,
        env={
            **os.environ,
            "PATH": f"{executable_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CARGO_CALL_LOG": str(call_log),
            **environment,
        },
        text=True,
        timeout=10,
    )


def _assert_isolated_cargo_context(context_log: Path, candidate: Path, rustup: Path) -> None:
    context = json.loads(context_log.read_text(encoding="utf-8"))
    assert Path(context["cwd"]).name.startswith("vibesys-cargo-work-")
    assert not Path(context["cwd"]).is_relative_to(candidate)
    assert Path(context["cargo_home"]).name.startswith("vibesys-cargo-home-")
    assert not Path(context["cargo_home"]).is_relative_to(candidate)
    assert context["rustup_home"] == str(rustup)
    assert context["cargo_wrapper"] is None
    assert context["rustflags"] is None
    assert context["git_config"] is None


def test_cargo_install_argv_uses_locked_revision_and_positional_package(tmp_path: Path) -> None:
    arguments = cargo_install_argv(_spec(), tmp_path / "install")

    assert arguments == (
        "cargo",
        "install",
        "--git",
        "https://example.com/tools",
        "--rev",
        "1" * 40,
        "--locked",
        "--root",
        str(tmp_path / "install"),
        "--bin",
        "runner",
        "--bin",
        "tracegen",
        "example-package",
    )
    assert "--package" not in arguments


def test_prepare_tools_publishes_complete_install_and_reuses_it(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def install(arguments):  # noqa: ANN001, ANN202
        normalized = tuple(arguments)
        calls.append(normalized)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return subprocess.CompletedProcess(normalized, 0, "", "")

    install_parent = tmp_path / "tools"
    first = prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)
    second = prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)

    root = tool_install_root(install_parent, "example", _spec())
    expected = root / "bin" / "runner"
    assert first[tool_token("example", "runner")] == str(expected)
    assert second == first
    assert len(calls) == 1
    assert expected.is_file()
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["spec"] == _spec().model_dump(mode="json")
    assert set(receipt["binaries"]) == {"runner", "tracegen"}
    assert root.name == tool_spec_digest(_spec())
    assert not list(root.parent.glob(f".{root.name}-*"))


def test_lifecycle_hooks_snapshot_tools_and_execute_target_command(tmp_path: Path) -> None:
    tools = {"example": _spec()}
    install_parent = tmp_path / "tools"
    hooks = EvaluatorToolLifecycleHooks(tools, install_parent)
    tools.clear()
    sandbox = MagicMock()
    sandbox.execute.return_value = MagicMock(exit_code=0, output="", truncated=False)

    lifecycle = SandboxLifecycle([hooks])
    lifecycle.before_ready(sandbox)
    lifecycle.before_ready(sandbox)

    assert sandbox.execute.call_count == 2
    command = sandbox.execute.call_args_list[0].args[0]
    assert sandbox.execute.call_args_list[0].kwargs == {"timeout": 660}
    assert sandbox.execute.call_args_list[1].args[0] == command
    arguments = shlex.split(command)
    assert arguments[0:2] == ["python3", "-c"]
    assert json.loads(arguments[-2])["tools"] == {"example": _spec().model_dump(mode="json")}
    assert arguments[-1] == str(install_parent)


@pytest.mark.parametrize("exit_code", [None, 17])
def test_lifecycle_hooks_reject_target_install_failure(
    tmp_path: Path,
    exit_code: int | None,
) -> None:
    sandbox = MagicMock()
    sandbox.execute.return_value = MagicMock(
        exit_code=exit_code,
        output=(
            "permission denied\n" + ("unhelpful install progress\n" * 1000) + "root sandbox failure"
        ),
        truncated=True,
    )
    lifecycle = SandboxLifecycle(
        [EvaluatorToolLifecycleHooks({"example": _spec()}, tmp_path / "tools")]
    )

    with pytest.raises(
        EvaluatorToolError,
        match=rf"sandbox installation failed \(exit {exit_code}\): permission denied",
    ) as error:
        lifecycle.hooks[0].before_ready(BeforeReadyContext(sandbox=sandbox))

    assert "root sandbox failure" in str(error.value)
    assert len(str(error.value)) < 2100


def test_target_install_command_quotes_manifest_and_root(tmp_path: Path) -> None:
    install_parent = tmp_path / "tools with 'quotes'; $(not-a-command)"

    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    arguments = shlex.split(command)

    assert arguments[0:2] == ["python3", "-c"]
    assert json.loads(arguments[-2]) == {
        "schema_version": 1,
        "tools": {"example": _spec().model_dump(mode="json")},
    }
    assert arguments[-1] == str(install_parent)


def test_target_install_command_rejects_unsafe_tool_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid evaluator tool name"):
        evaluator_tools_install_command({"../escape": _spec()}, tmp_path / "tools")


def test_target_install_command_rejects_symlinked_cache_root(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    install_parent = tmp_path / "tools"
    install_parent.symlink_to(escaped, target_is_directory=True)
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)

    result = _run_target_command(command, executable_dir, call_log)

    assert result.returncode == 1
    assert "install root is a symlink" in result.stderr
    assert not list(escaped.iterdir())


def test_target_install_command_publishes_and_reuses_verified_tool(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "target tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)

    first = _run_target_command(command, executable_dir, call_log)
    second = _run_target_command(command, executable_dir, call_log)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == ["call"]
    root = tool_install_root(install_parent, "example", _spec())
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["spec"] == _spec().model_dump(mode="json")
    assert set(receipt["binaries"]) == {"runner", "tracegen"}
    assert os.access(root / "bin" / "runner", os.X_OK)
    assert not list(root.parent.glob(f".{root.name}-*"))


def test_target_install_command_anchors_relative_root_before_cargo_cwd_change(
    tmp_path: Path,
) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = evaluator_tools_install_command({"example": _spec()}, Path("tools"))

    result = _run_target_command(command, executable_dir, call_log, cwd=workspace)

    assert result.returncode == 0, result.stderr
    root = tool_install_root(workspace / "tools", "example", _spec())
    assert (root / "bin" / "runner").is_file()
    assert (root / "bin" / "tracegen").is_file()


def test_target_install_receipt_is_reused_by_host_installer(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    target_result = _run_target_command(command, executable_dir, call_log)
    host_runner = MagicMock()

    replacements = prepare_evaluator_tools(
        {"example": _spec()},
        install_parent,
        command_runner=host_runner,
    )

    assert target_result.returncode == 0, target_result.stderr
    host_runner.assert_not_called()
    expected = tool_install_root(install_parent, "example", _spec()) / "bin" / "runner"
    assert replacements[tool_token("example", "runner")] == str(expected)


def test_target_install_isolates_cargo_from_candidate_configuration(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    candidate = tmp_path / "candidate"
    (candidate / ".cargo").mkdir(parents=True)
    (candidate / ".cargo" / "config.toml").write_text(
        '[build]\nrustc-wrapper = "./poison"\n',
        encoding="utf-8",
    )
    context_log = tmp_path / "cargo-context.json"
    rustup = tmp_path / "trusted-rustup"

    result = _run_target_command(
        evaluator_tools_install_command({"example": _spec()}, tmp_path / "tools"),
        executable_dir,
        call_log,
        cwd=candidate,
        FAKE_CARGO_CONTEXT_LOG=str(context_log),
        CARGO_BUILD_RUSTC_WRAPPER="./candidate-wrapper",
        RUSTFLAGS="--cfg candidate",
        GIT_CONFIG_COUNT="1",
        RUSTUP_HOME=str(rustup),
    )

    assert result.returncode == 0, result.stderr
    _assert_isolated_cargo_context(context_log, candidate, rustup)


def test_host_install_isolates_cargo_from_candidate_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    candidate = tmp_path / "candidate"
    (candidate / ".cargo").mkdir(parents=True)
    (candidate / ".cargo" / "config.toml").write_text(
        '[build]\nrustc-wrapper = "./poison"\n',
        encoding="utf-8",
    )
    context_log = tmp_path / "cargo-context.json"
    rustup = tmp_path / "trusted-rustup"
    monkeypatch.chdir(candidate)
    monkeypatch.setenv("PATH", f"{executable_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CARGO_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_CARGO_CONTEXT_LOG", str(context_log))
    monkeypatch.setenv("CARGO_BUILD_RUSTC_WRAPPER", "./candidate-wrapper")
    monkeypatch.setenv("RUSTFLAGS", "--cfg candidate")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("RUSTUP_HOME", str(rustup))

    prepare_evaluator_tools({"example": _spec()}, tmp_path / "tools")

    _assert_isolated_cargo_context(context_log, candidate, rustup)


def test_target_install_command_rejects_changed_binary(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    first = _run_target_command(command, executable_dir, call_log)
    root = tool_install_root(install_parent, "example", _spec())
    runner = root / "bin" / "runner"
    runner.chmod(0o755)
    runner.write_text("tampered", encoding="utf-8")

    second = _run_target_command(command, executable_dir, call_log)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 1
    assert "failed receipt verification" in second.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == ["call"]


def test_target_install_command_rejects_symlinked_binary(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)
    first = _run_target_command(command, executable_dir, call_log)
    root = tool_install_root(install_parent, "example", _spec())
    runner = root / "bin" / "runner"
    relocated = tmp_path / "relocated-runner"
    (root / "bin").chmod(0o755)
    runner.replace(relocated)
    runner.symlink_to(relocated)

    second = _run_target_command(command, executable_dir, call_log)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 1
    assert "failed receipt verification" in second.stderr


def test_target_install_command_bounds_cargo_failure_and_cleans_staging(
    tmp_path: Path,
) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)

    result = _run_target_command(
        command,
        executable_dir,
        call_log,
        FAKE_CARGO_EXIT="7",
        FAKE_CARGO_STDERR="unhelpful compiler progress\n" * 1000 + "root compiler failure",
    )

    assert result.returncode == 1
    assert result.stderr.startswith("cannot install evaluator tool 'example':")
    assert "root compiler failure" in result.stderr
    assert len(result.stderr) <= 2001
    cache = install_parent / "example"
    assert not list(cache.glob(".*-*"))


def test_target_install_command_reports_missing_cargo(tmp_path: Path) -> None:
    python_only = tmp_path / "python-only"
    python_only.mkdir()
    (python_only / "python3").symlink_to(sys.executable)
    command = evaluator_tools_install_command({"example": _spec()}, tmp_path / "tools")

    result = subprocess.run(  # noqa: S603
        shlex.split(command),
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": str(python_only)},
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "cargo was not found" in result.stderr


def test_target_install_command_rejects_missing_declared_binary(tmp_path: Path) -> None:
    executable_dir, call_log = _fake_cargo(tmp_path)
    install_parent = tmp_path / "tools"
    command = evaluator_tools_install_command({"example": _spec()}, install_parent)

    result = _run_target_command(
        command,
        executable_dir,
        call_log,
        FAKE_CARGO_SKIP="tracegen",
        FAKE_CARGO_STDERR="cargo reported success without every requested binary",
    )

    assert result.returncode == 1
    assert "did not install every declared binary" in result.stderr
    assert "cargo reported success without every requested binary" in result.stderr
    cache = install_parent / "example"
    assert not list(cache.glob(".*-*"))


def test_prepare_tools_rejects_binary_changed_after_receipt(tmp_path: Path) -> None:
    def install(arguments):  # noqa: ANN001, ANN202
        normalized = tuple(arguments)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return subprocess.CompletedProcess(normalized, 0, "", "")

    install_parent = tmp_path / "tools"
    prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)
    root = tool_install_root(install_parent, "example", _spec())
    (root / "bin" / "runner").write_text("tampered", encoding="utf-8")

    with pytest.raises(EvaluatorToolError, match="failed receipt verification"):
        prepare_evaluator_tools({"example": _spec()}, install_parent, command_runner=install)


def test_prepare_tools_accepts_verified_concurrent_winner(tmp_path: Path) -> None:
    install_parent = tmp_path / "tools"

    def write_binaries(arguments):  # noqa: ANN001, ANN202
        normalized = tuple(arguments)
        root = Path(normalized[normalized.index("--root") + 1])
        for binary in ("runner", "tracegen"):
            path = root / "bin" / binary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("binary", encoding="utf-8")
            path.chmod(0o755)
        return subprocess.CompletedProcess(normalized, 0, "", "")

    def publish_winner(arguments):  # noqa: ANN001, ANN202
        result = write_binaries(arguments)
        prepare_evaluator_tools(
            {"example": _spec()},
            install_parent,
            command_runner=write_binaries,
        )
        return result

    replacements = prepare_evaluator_tools(
        {"example": _spec()},
        install_parent,
        command_runner=publish_winner,
    )

    root = tool_install_root(install_parent, "example", _spec())
    assert replacements[tool_token("example", "runner")] == str(root / "bin" / "runner")
    assert json.loads((root / "receipt.json").read_text(encoding="utf-8"))["spec"] == (
        _spec().model_dump(mode="json")
    )


def test_prepare_tools_translates_missing_cargo(tmp_path: Path) -> None:
    def missing(arguments):  # noqa: ANN001, ANN202, ARG001
        raise FileNotFoundError("cargo")

    with pytest.raises(EvaluatorToolError, match="cargo was not found"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=missing)


def test_prepare_tools_reports_cargo_failure_and_cleans_staging(tmp_path: Path) -> None:
    def fail(arguments):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(
            arguments,
            7,
            "",
            "unhelpful compiler progress\n" * 1000 + "dependency resolution failed",
        )

    with pytest.raises(EvaluatorToolError, match="dependency resolution failed"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=fail)

    cache = tmp_path / "example"
    assert not list(cache.glob(".*-*"))


def test_prepare_tools_reports_timeout_and_cleans_staging(tmp_path: Path) -> None:
    def timeout(arguments):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(arguments, 600)

    with pytest.raises(EvaluatorToolError, match="cargo install timed out"):
        prepare_evaluator_tools({"example": _spec()}, tmp_path, command_runner=timeout)

    cache = tmp_path / "example"
    assert not list(cache.glob(".*-*"))
