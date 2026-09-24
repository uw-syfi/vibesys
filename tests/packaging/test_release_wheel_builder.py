"""Contracts for assembling one native self-contained release wheel."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from build_release_wheel import (
    BuildEnvironment,
    ReleaseBuildError,
    build_release_wheel,
)
from tests.support import run_test_command

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_build_release_wheel_preserves_the_public_positional_interface() -> None:
    """A required repository-root argument must not break release callers."""
    parameters = list(inspect.signature(build_release_wheel).parameters.values())

    assert [parameter.name for parameter in parameters[:3]] == [
        "target_key",
        "bun",
        "output_dir",
    ]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters[:3]
    )


def test_build_script_can_be_invoked_by_file_path(tmp_path: Path) -> None:
    """The documented ``python scripts/...`` entry point must import its helpers."""
    result = run_test_command(
        [sys.executable, str(PROJECT_ROOT / "packaging/build_release_wheel.py"), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "--target TARGET" in result.stdout


def _make_repo(root: Path) -> tuple[Path, Path]:
    tui = root / "clients" / "tui"
    (tui / "dist").mkdir(parents=True)
    (tui / "package.json").write_text(json.dumps({"name": "@vibesys/tui", "version": "0.1.0"}))
    (tui / "dist" / "launcher.js").write_text("// launcher\n")
    (tui / "dist" / "self-test.js").write_text("// self-test\n")
    (root / "pyproject.toml").write_text('[project]\nname = "vibesys"\nversion = "0.1.0"\n')
    license_path = root / "third_party" / "bun" / "LICENSE"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("Bun license\n")
    bun = root / "tools" / "bun"
    bun.parent.mkdir()
    bun.write_text("binary")
    bun.chmod(0o755)
    return root, bun


def _fake_deployment(destination: Path) -> None:
    (destination / "dist").mkdir(parents=True)
    (destination / "dist" / "launcher.js").write_text("// launcher\n")
    (destination / "dist" / "launcher.js.map").write_text("{}\n")
    (destination / "dist" / "self-test.js").write_text("// self-test\n")
    (destination / "package.json").write_text(
        json.dumps(
            {
                "name": "@vibesys/tui",
                "version": "0.1.0",
                "dependencies": {
                    "@vibesys/backend-client": "@vibesys/backend-client@file:///build/backend",
                    "@vibesys/core-state": "@vibesys/core-state@file:///build/core",
                },
            }
        )
    )
    for package in ("backend-client", "core-state"):
        workspace_package = destination / "node_modules" / "@vibesys" / package
        (workspace_package / "dist").mkdir(parents=True)
        (workspace_package / "package.json").write_text(
            json.dumps({"name": f"@vibesys/{package}", "version": "0.1.0"})
        )
        (workspace_package / "dist" / "index.js").write_text(f"// {package}\n")
    opentui = destination / "node_modules" / "@opentui"
    (opentui / "core").mkdir(parents=True)
    (opentui / "core" / "index.js").write_text("// core\n")
    (opentui / "core" / "LICENSE").write_text("OpenTUI license\n")
    for package in ("core-linux-x64", "core-linux-arm64", "core-darwin-arm64"):
        native = opentui / package
        native.mkdir()
        (native / "index.js").write_text("// native\n")


def test_build_release_wheel_assembles_payload_without_mutating_node_modules(
    tmp_path: Path,
) -> None:
    repo, bun = _make_repo(tmp_path / "repo")
    sentinel = repo / "clients" / "node_modules" / "sentinel"
    sentinel.parent.mkdir()
    sentinel.write_text("untouched\n")
    output = tmp_path / "dist"
    calls: list[tuple[list[str], Path, Mapping[str, str] | None]] = []
    payload_snapshot: dict[str, object] = {}

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        argv = [str(part) for part in command]
        calls.append((argv, cwd, env))
        if argv == [str(bun), "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1.3.9\n", stderr="")
        if "deploy" in argv:
            _fake_deployment(Path(argv[-1]))
        if argv[:3] == ["uv", "build", "--wheel"]:
            assert env is not None
            payload = Path(env["VIBESYS_TUI_BUNDLE"])
            payload_snapshot["manifest"] = json.loads((payload / "manifest.json").read_text())
            payload_snapshot["files"] = {
                path.relative_to(payload).as_posix() for path in payload.rglob("*")
            }
            payload_snapshot["package"] = json.loads((payload / "app" / "package.json").read_text())
            wheel = output / "vibesys-0.1.0-py3-none-manylinux_2_28_x86_64.whl"
            wheel.parent.mkdir(parents=True, exist_ok=True)
            wheel.write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    wheel = build_release_wheel(
        "linux-x86_64",
        bun,
        output,
        environment=BuildEnvironment(
            runner=runner,
            host_system="Linux",
            host_machine="x86_64",
        ),
        _repo_root=repo,
    )

    assert wheel.name == "vibesys-0.1.0-py3-none-manylinux_2_28_x86_64.whl"
    assert sentinel.read_text() == "untouched\n"
    commands = [call[0] for call in calls]
    workspace = repo.resolve() / "clients"
    assert [(c[0], c[1]) for c in calls if c[0][0] == "pnpm" and "deploy" not in c[0]] == [
        (["pnpm", "install", "--frozen-lockfile"], workspace),
        (["pnpm", "build:clients"], workspace),
    ]
    deploy = next(command for command in commands if "deploy" in command)
    assert "--prod" in deploy
    assert "--legacy" not in deploy
    build_call = next(call for call in calls if call[0][:3] == ["uv", "build", "--wheel"])
    assert build_call[2] is not None
    assert build_call[2]["VIBESYS_WHEEL_TARGET"] == "linux-x86_64"
    manifest = payload_snapshot["manifest"]
    assert isinstance(manifest, dict)
    assert manifest["bun_version"] == "1.3.9"
    assert manifest["target"] == "linux-x86_64"
    files = payload_snapshot["files"]
    assert isinstance(files, set)
    assert "app/dist/launcher.js.map" not in files
    assert "app/node_modules/@opentui/core-linux-x64" in files
    assert "app/node_modules/@opentui/core-linux-arm64" not in files
    assert "app/node_modules/@vibesys/backend-client/dist/index.js" in files
    assert "app/node_modules/@vibesys/core-state/dist/index.js" in files
    package = payload_snapshot["package"]
    assert isinstance(package, dict)
    assert package["dependencies"] == {
        "@vibesys/backend-client": "0.1.0",
        "@vibesys/core-state": "0.1.0",
    }
    assert "file://" not in json.dumps(package)


def test_build_release_wheel_resolves_caller_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing subprocess cwd must not reinterpret caller-relative paths."""
    repo, bun = _make_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    relative_bun = bun.relative_to(tmp_path)
    relative_output = Path("release")
    calls: list[tuple[list[str], Path]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, env
        argv = [str(part) for part in command]
        command_cwd = Path(cwd)
        calls.append((argv, command_cwd))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout="1.3.9\n", stderr="")
        if "deploy" in argv:
            _fake_deployment(Path(argv[-1]))
        if argv[:3] == ["uv", "build", "--wheel"]:
            output_argument = Path(argv[-1])
            command_output = (
                output_argument if output_argument.is_absolute() else command_cwd / output_argument
            )
            command_output.mkdir(parents=True, exist_ok=True)
            (command_output / "vibesys-0.1.0-py3-none-manylinux_2_28_x86_64.whl").write_bytes(
                b"wheel"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    wheel = build_release_wheel(
        "linux-x86_64",
        relative_bun,
        relative_output,
        environment=BuildEnvironment(
            runner=runner,
            host_system="Linux",
            host_machine="x86_64",
        ),
        _repo_root=repo,
    )

    expected_output = (tmp_path / relative_output).resolve()
    assert wheel == expected_output / wheel.name
    assert calls[0][0] == [str(bun.resolve()), "--version"]
    build_command = next(
        command for command, _cwd in calls if command[:3] == ["uv", "build", "--wheel"]
    )
    assert build_command[-1] == str(expected_output)


def test_build_release_wheel_rejects_the_wrong_bun_version(tmp_path: Path) -> None:
    repo, bun = _make_repo(tmp_path / "repo")

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check, capture_output, text, env
        return subprocess.CompletedProcess(command, 0, stdout="1.3.8\n", stderr="")

    with pytest.raises(ReleaseBuildError, match=r"Bun 1\.3\.9"):
        build_release_wheel(
            "linux-x86_64",
            bun,
            tmp_path / "dist",
            environment=BuildEnvironment(
                runner=runner,
                host_system="Linux",
                host_machine="x86_64",
            ),
            _repo_root=repo,
        )


def test_build_release_wheel_rejects_preexisting_wheels(tmp_path: Path) -> None:
    repo, bun = _make_repo(tmp_path / "repo")
    output = tmp_path / "dist"
    output.mkdir()
    (output / "old.whl").write_bytes(b"old")

    with pytest.raises(ReleaseBuildError, match="already contains"):
        build_release_wheel(
            "linux-x86_64",
            bun,
            output,
            environment=BuildEnvironment(
                host_system="Linux",
                host_machine="x86_64",
            ),
            _repo_root=repo,
        )


def test_build_release_wheel_rejects_a_universal_wheel_tag(tmp_path: Path) -> None:
    repo, bun = _make_repo(tmp_path / "repo")
    output = tmp_path / "dist"

    def runner(
        command: Sequence[str],
        *,
        cwd: Path,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check, capture_output, text, env
        argv = [str(part) for part in command]
        if argv == [str(bun), "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1.3.9\n", stderr="")
        if "deploy" in argv:
            _fake_deployment(Path(argv[-1]))
        if argv[:3] == ["uv", "build", "--wheel"]:
            output.mkdir(parents=True, exist_ok=True)
            (output / "vibesys-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(ReleaseBuildError, match="platform tag"):
        build_release_wheel(
            "linux-x86_64",
            bun,
            output,
            environment=BuildEnvironment(
                runner=runner,
                host_system="Linux",
                host_machine="x86_64",
            ),
            _repo_root=repo,
        )
