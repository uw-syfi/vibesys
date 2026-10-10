"""Evaluator tool installation against real processes and a generated fake Cargo.

The unit tests in ``libs/vs-sandbox/tests/test_evaluator_tools.py`` drive installation through
an injected command runner. What only real processes show is here: the generated install
command run by a real shell and interpreter, Cargo's isolated environment, and the host
installer's real ``cargo`` invocation.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    evaluator_tools_install_command,
    prepare_evaluator_tools,
    tool_install_root,
    tool_token,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


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
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-948006 [S603]; Test executes its generated fixed interpreter argv against a fake Cargo binary.
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
    host_calls: list[tuple[str, ...]] = []

    def host_runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        host_calls.append(tuple(arguments))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    replacements = prepare_evaluator_tools(
        {"example": _spec()},
        install_parent,
        command_runner=host_runner,
    )

    assert target_result.returncode == 0, target_result.stderr
    assert host_calls == []
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

    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-948007 [S603]; Test executes its generated fixed interpreter argv with an intentionally Cargo-free PATH.
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
