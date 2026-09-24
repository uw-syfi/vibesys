"""Assemble a self-contained native TUI payload and build one release wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

from release_versions import (
    ReleaseVersionSyntaxError,
    npm_release_identity,
    python_release_identity,
)
from tui_packaging import BUN_VERSION, validate_tui_payload
from wheel_targets import resolve_wheel_target

REPO_ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[..., subprocess.CompletedProcess[str]]
_WORKSPACE_RUNTIME_PACKAGES = ("@vibesys/backend-client", "@vibesys/core-state")


class ReleaseBuildError(RuntimeError):
    """Raised when a release payload or wheel cannot be built deterministically."""

    @classmethod
    def wrong_bun(cls, actual: str) -> ReleaseBuildError:
        """Build an error for a runtime version mismatch."""
        return cls(f"Release wheels require Bun {BUN_VERSION}, found {actual or 'no version'}")

    @classmethod
    def command_failed(cls, command: Sequence[str], returncode: int) -> ReleaseBuildError:
        """Build an error for a failed external build command."""
        return cls(f"Command failed with exit code {returncode}: {' '.join(command)}")


@dataclass(frozen=True)
class BuildEnvironment:
    """Injectable host facts and process runner for a release build."""

    runner: Runner = subprocess.run
    host_system: str | None = None
    host_machine: str | None = None


def build_release_wheel(
    target_key: str,
    bun: Path,
    output_dir: Path,
    *,
    environment: BuildEnvironment | None = None,
    _repo_root: Path = REPO_ROOT,
) -> Path:
    """Build exactly one native wheel for ``target_key`` and return its path."""
    repo_root = _repo_root.resolve()
    bun = bun.resolve()
    output_dir = output_dir.resolve()
    build_environment = environment or BuildEnvironment()
    target = resolve_wheel_target(
        target_key,
        host_system=build_environment.host_system or platform.system(),
        host_machine=build_environment.host_machine or platform.machine(),
    )
    if not bun.is_file() or not bun.stat().st_mode & stat.S_IXUSR:
        _fail(f"Bun executable is missing or not executable: {bun}")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_wheels = sorted(output_dir.glob("*.whl"))
    if existing_wheels:
        _fail(f"Output directory already contains wheels: {output_dir}")
    version_result = _run(
        [str(bun), "--version"],
        cwd=repo_root,
        runner=build_environment.runner,
        capture_output=True,
        text=True,
    )
    actual_bun_version = version_result.stdout.strip()
    if actual_bun_version != BUN_VERSION:
        error = ReleaseBuildError.wrong_bun(actual_bun_version)
        raise error
    distribution_version, tui_version = _project_versions(repo_root)
    try:
        distribution_identity = python_release_identity(
            distribution_version,
            source="Python project version",
        )
        tui_identity = npm_release_identity(tui_version, source="TUI project version")
    except ReleaseVersionSyntaxError as exc:
        raise ReleaseBuildError(str(exc)) from exc
    if distribution_identity != tui_identity:
        _fail(f"Python version {distribution_version} does not match TUI version {tui_version}")
    workspace = repo_root / "clients"
    _run(
        ["pnpm", "install", "--frozen-lockfile"],
        cwd=workspace,
        runner=build_environment.runner,
    )
    _run(
        ["pnpm", "build:clients"],
        cwd=workspace,
        runner=build_environment.runner,
    )
    with tempfile.TemporaryDirectory(prefix=f"vibesys-{target.key}-") as temporary:
        payload = Path(temporary) / "payload"
        app = payload / "app"
        deploy_command = [
            "pnpm",
            "--config.node-linker=hoisted",
            "--filter",
            "@vibesys/tui",
            "deploy",
            "--prod",
            str(app),
        ]
        _run(deploy_command, cwd=workspace, runner=build_environment.runner)
        _normalize_deployed_workspace_dependencies(app)
        _prune_deployment(app, expected_native_package=target.opentui_package)
        _stage_runtime_and_licenses(repo_root, bun=bun, payload=payload)
        _write_manifest(
            payload,
            target_key=target.key,
            tui_version=tui_version,
        )
        validate_tui_payload(
            payload,
            expected_target=target.key,
            expected_distribution_version=distribution_version,
        )
        env = {
            **os.environ,
            "VIBESYS_TUI_BUNDLE": str(payload),
            "VIBESYS_WHEEL_TARGET": target.key,
        }
        _run(
            ["uv", "build", "--wheel", "--out-dir", str(output_dir)],
            cwd=repo_root,
            runner=build_environment.runner,
            env=env,
        )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        _fail(f"Expected exactly one wheel in {output_dir}, found {len(wheels)}")
    expected_suffix = f"-py3-none-{target.wheel_platform}.whl"
    if not wheels[0].name.endswith(expected_suffix):
        _fail(
            f"Wheel has the wrong platform tag for {target.key}: {wheels[0].name}; "
            f"expected {expected_suffix}"
        )
    return wheels[0]


def _project_versions(repo_root: Path) -> tuple[str, str]:
    root_data = tomllib.loads((repo_root / "pyproject.toml").read_text())
    raw_tui_data = json.loads((repo_root / "clients" / "tui" / "package.json").read_text())
    tui_data = cast("dict[str, object]", raw_tui_data) if isinstance(raw_tui_data, dict) else {}
    distribution_version = root_data.get("project", {}).get("version")
    tui_version = tui_data.get("version")
    if not isinstance(distribution_version, str) or not isinstance(tui_version, str):
        _fail("Python and TUI projects must declare string versions")
    return distribution_version, tui_version


def _normalize_deployed_workspace_dependencies(app: Path) -> None:
    """Replace pnpm's checkout-specific workspace URLs with package versions."""
    package_path = app / "package.json"
    try:
        package = json.loads(package_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        error = ReleaseBuildError("Production deployment has an invalid package.json")
        raise error from exc
    if not isinstance(package, dict) or not isinstance(package.get("dependencies"), dict):
        _fail("Production deployment has no dependency metadata")
    dependencies = cast("dict[str, object]", package["dependencies"])
    for name in _WORKSPACE_RUNTIME_PACKAGES:
        dependency_path = app / "node_modules" / Path(*name.split("/")) / "package.json"
        try:
            dependency = json.loads(dependency_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            error = ReleaseBuildError(f"Production deployment has an invalid {name} package")
            raise error from exc
        version = dependency.get("version") if isinstance(dependency, dict) else None
        if not isinstance(version, str):
            _fail(f"Production deployment {name} package has no version")
        dependencies[name] = version
    package_path.write_text(json.dumps(package, indent=2, sort_keys=True) + "\n")


def _prune_deployment(app: Path, *, expected_native_package: str) -> None:
    if not (app / "dist" / "launcher.js").is_file():
        _fail("TUI build did not produce dist/launcher.js")
    if not (app / "dist" / "self-test.js").is_file():
        _fail("TUI build did not produce dist/self-test.js")
    opentui_root = app / "node_modules" / "@opentui"
    if not opentui_root.is_dir():
        _fail("Production deployment has no @opentui dependencies")
    for package in opentui_root.iterdir():
        qualified_name = f"@opentui/{package.name}"
        if package.name.startswith("core-") and qualified_name != expected_native_package:
            _remove_path(package)
    expected_path = opentui_root / expected_native_package.removeprefix("@opentui/")
    if not expected_path.is_dir():
        _fail(f"Production deployment is missing {expected_native_package}")
    _remove_path(app / "node_modules" / ".bin")
    for source_map in app.rglob("*.map"):
        source_map.unlink()


def _stage_runtime_and_licenses(repo_root: Path, *, bun: Path, payload: Path) -> None:
    runtime = payload / "bin" / "bun"
    runtime.parent.mkdir(parents=True)
    shutil.copy2(bun, runtime)
    runtime.chmod(0o755)
    licenses = payload / "licenses"
    licenses.mkdir()
    bun_license = repo_root / "third_party" / "bun" / "LICENSE"
    opentui_license = payload / "app" / "node_modules" / "@opentui" / "core" / "LICENSE"
    if not bun_license.is_file():
        _fail(f"Bun license is missing: {bun_license}")
    if not opentui_license.is_file():
        _fail(f"OpenTUI license is missing: {opentui_license}")
    shutil.copy2(bun_license, licenses / "BUN-LICENSE.md")
    shutil.copy2(opentui_license, licenses / "opentui-core.txt")


def _write_manifest(payload: Path, *, target_key: str, tui_version: str) -> None:
    files = {
        path.relative_to(payload).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(payload.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "schema_version": 1,
        "target": target_key,
        "bun_version": BUN_VERSION,
        "tui_version": tui_version,
        "files": files,
    }
    (payload / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: Runner,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(list(command), cwd=cwd, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        error = ReleaseBuildError.command_failed(command, exc.returncode)
        raise error from exc


def _fail(message: str) -> Never:
    raise ReleaseBuildError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", required=True, help="Target key from packaging/wheel_targets.py"
    )
    parser.add_argument("--bun", required=True, type=Path, help="Pinned Bun executable")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    return parser.parse_args()


def main() -> int:
    """Build the requested release wheel from the repository root."""
    args = _parse_args()
    wheel = build_release_wheel(args.target, args.bun, args.output_dir)
    print(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
