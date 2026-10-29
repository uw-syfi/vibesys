"""Verify a VibeSys tool installation without access to its source checkout."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import site
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Never

from vibesys.api.request import default_skill_roots
from vibesys.config import BUNDLED_RESOURCES
from vibesys.orchestration.profilers import ACTIVE_PROFILER_KINDS
from vs_runtime.api.infrastructure import (
    EvaluatorPackageRequirement,
    SDKRoots,
    materialize_input_project,
    resolve_evaluator_package,
    resolve_packaged_tree,
)

FRAMEWORK_PACKAGES = (
    "vibesys",
    "entrypoints",
    "server",
    "headless",
    "launch",
    "vs_agent",
    "vs_async_ops",
    "vs_evaluator_protocol",
    "vs_decode",
    "vs_redaction",
    "vs_evaluation",
    "vs_github",
    "vs_issue_tracker",
    "vs_core",
    "vs_project",
    "vs_prompts",
    "vs_runtime",
    "vs_sandbox",
    "vs_slurm",
    "vs_mcp",
    "vs_faults",
    "vs_sim",
    "vs_logs",
)
REQUIRED_SYSTEM_TOOLS = ("git",)


def resolved_runtime_root(path: Path | None = None) -> Path:
    """Resolve the clean-room root, including macOS's ``/tmp`` symlink."""
    configured = path or Path(os.environ.get("VIBESYS_RELEASE_RUNTIME_ROOT", tempfile.gettempdir()))
    return configured.resolve()


_RUNTIME_ROOT = resolved_runtime_root()


class InstalledReleaseError(RuntimeError):
    """Raised when an installed release is incomplete or contaminated."""

    @classmethod
    def command_failed(cls, command: list[str]) -> InstalledReleaseError:
        """Build an error for a failed installed-artifact command."""
        return cls(f"Installed verification command failed: {command}")


def verify_installed_release() -> None:
    """Exercise the installed framework, SDK, and resources."""
    _verify_isolated_interpreter()
    _verify_framework_imports()
    verify_console_entry_point()
    _verify_resources()
    _verify_materialized_sdk()


def _verify_isolated_interpreter() -> None:
    if os.environ.get("PYTHONNOUSERSITE") != "1" or site.ENABLE_USER_SITE:
        _fail("User-site packages are not disabled")
    if os.environ.get("PYTHONPATH"):
        _fail("PYTHONPATH must be empty")
    _verify_required_system_tools()
    home = Path(os.environ.get("HOME", ""))
    if not home.is_dir() or any(home.iterdir()):
        _fail(f"HOME must be an empty isolated directory: {home}")
    if Path.cwd().resolve() != _RUNTIME_ROOT:
        _fail(f"Installed verification must run from {_RUNTIME_ROOT}, not {Path.cwd().resolve()}")


def _verify_required_system_tools() -> None:
    for executable in REQUIRED_SYSTEM_TOOLS:
        if shutil.which(executable) is None:
            _fail(f"Required system executable is absent from PATH: {executable}")


def _verify_framework_imports() -> None:
    distributions = importlib.metadata.packages_distributions()
    for package in FRAMEWORK_PACKAGES:
        module = importlib.import_module(package)
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            _fail(f"Installed package has no import path: {package}")
        resolved = Path(module_file).resolve()
        if "site-packages" not in str(resolved):
            _fail(f"Package did not import from site-packages: {package} -> {resolved}")
        if distributions.get(package) != ["vibesys"]:
            _fail(
                f"Package {package} is not owned only by the vibesys distribution: "
                f"{distributions.get(package)}"
            )


def verify_console_entry_point() -> None:
    """Exercise the installed VibeSys console scripts."""
    executable = shutil.which("vibesys")
    if executable is None:
        _fail("Installed vibesys console script is absent from PATH")
    _run([executable, "--help"], timeout=30)


def _verify_resources() -> None:
    root = BUNDLED_RESOURCES.root()
    if root is None or "site-packages" not in str(root.resolve()):
        _fail(f"Installed resources did not resolve from site-packages: {root}")
    skill_roots = default_skill_roots()
    if len(skill_roots) != 1 or not any(skill_roots[0].rglob("SKILL.md")):
        _fail(f"Installed skills did not resolve: {skill_roots}")
    for kind in ACTIVE_PROFILER_KINDS:
        support = BUNDLED_RESOURCES.directory("profilers", kind.value)
        if support is None or "site-packages" not in str(support.resolve()):
            _fail(f"Installed profiler resources did not resolve for {kind.value}: {support}")
    evaluator_packages = BUNDLED_RESOURCES.directory("evaluators")
    if evaluator_packages is None:
        _fail("Installed evaluator package resources did not resolve")
    for name in ("vibesys-evaluator-microservice", "vibesys-evaluator-queue"):
        package = resolve_evaluator_package(
            evaluator_packages,
            EvaluatorPackageRequirement(name=name, version="0.1.0"),
        )
        if "site-packages" not in str(package.root.resolve()):
            _fail(f"Installed evaluator package did not resolve for {name}: {package.root}")


def _verify_materialized_sdk() -> None:
    with tempfile.TemporaryDirectory(
        prefix="vibesys-installed-input-", dir=_RUNTIME_ROOT
    ) as temporary:
        root = Path(temporary)
        project_root = root / "missing-checkout"
        input_project = project_root / "examples" / "model-serving" / "input"
        input_project.mkdir(parents=True)
        (input_project / "pyproject.toml").write_text(
            "[project]\n"
            "name = 'installed-release-input'\n"
            "version = '0.1.0'\n"
            "requires-python = '>=3.12'\n"
            "dependencies = ['vs-bench']\n"
            "\n"
            "[tool.uv.sources]\n"
            "vs-bench = { path = '../../../sdk/vs-bench' }\n"
        )
        workspace = root / "workspace"
        workspace.mkdir()
        sdk_roots = SDKRoots(
            checkout=project_root / "sdk",
            packaged=resolve_packaged_tree(package="vibesys", packaged_subdir="_sdk"),
        )
        dependencies = materialize_input_project(
            input_project,
            workspace,
            sdk_roots=sdk_roots,
            copy_directory=_copy_tree,
        )
        if [dependency.name for dependency in dependencies] != ["vs-bench"]:
            _fail(f"Packaged SDK did not materialize: {dependencies}")
        source_path = dependencies[0].source_path.resolve()
        if "site-packages" not in str(source_path):
            _fail(f"SDK source did not resolve from the installed wheel: {source_path}")

        _run(build_sdk_sync_command(workspace), timeout=180)
        sdk_python = workspace / ".venv" / "bin" / "python"
        _run(
            [
                str(sdk_python),
                "-c",
                "import vs_bench; print(vs_bench.__file__)",
            ],
            timeout=30,
        )


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True)


def build_sdk_sync_command(workspace: Path) -> list[str]:
    """Build the nested SDK sync command with the isolated interpreter."""
    return [
        "uv",
        "sync",
        "--no-cache",
        "--no-config",
        "--python",
        sys.executable,
        "--project",
        str(workspace),
    ]


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int,
) -> None:
    try:
        # lint-waiver: LW-008034 [S603]; Release-verification commands are assembled internally as argv vectors and run without a shell.
        subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=env,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise InstalledReleaseError.command_failed(command) from exc


def _fail(message: str) -> Never:
    raise InstalledReleaseError(message)


def main() -> int:
    """Run installed verification with concise diagnostics."""
    try:
        verify_installed_release()
    except InstalledReleaseError as exc:
        print(f"installed release verification failed: {exc}", file=sys.stderr)
        return 1
    print("installed release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
