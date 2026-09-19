"""Verify a VibeSys tool installation without access to its source checkout."""

from __future__ import annotations

import errno
import importlib
import importlib.metadata
import json
import os
import pty
import select
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Never, cast

from entrypoints.launcher import bundled_tui
from vibesys.evaluators import EvaluatorPackageRequirement, resolve_evaluator_package
from vibesys.input_project import materialize_input_project
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.profilers import ACTIVE_PROFILER_KINDS
from vibesys.resource_paths import (
    default_skill_roots,
    profiler_support_dir,
    resources_root,
)
from vs_project import Project, ProjectError

FRAMEWORK_PACKAGES = (
    "vibesys",
    "entrypoints",
    "server",
    "vs_evaluator_protocol",
    "vs_feature_flags",
    "vs_github",
    "vs_issue_board",
    "vs_loop_state",
    "vs_project",
    "vs_prompts",
    "vs_sandbox",
)
REQUIRED_SYSTEM_TOOLS = ("git",)
SYSTEM_JAVASCRIPT_TOOLS = ("bun", "node", "npm", "pnpm")
TUI_SMOKE_MARKER_ENV = "VIBESYS_RELEASE_SMOKE_MARKER"
TUI_SMOKE_MARKER_CONTENT = "renderer initialized; control protocol exchanged\n"
PTY_OUTPUT_LIMIT = 64 * 1024


def resolved_runtime_root(path: Path | None = None) -> Path:
    """Resolve the clean-room root, including macOS's ``/tmp`` symlink."""
    configured = path or Path(os.environ.get("VIBESYS_RELEASE_RUNTIME_ROOT", "/tmp"))  # noqa: S108
    return configured.resolve()


_RUNTIME_ROOT = resolved_runtime_root()


class InstalledReleaseError(RuntimeError):
    """Raised when an installed release is incomplete or contaminated."""

    @classmethod
    def command_failed(cls, command: list[str]) -> InstalledReleaseError:
        """Build an error for a failed installed-artifact command."""
        return cls(f"Installed verification command failed: {command}")

    @classmethod
    def interactive_failed(
        cls,
        command: list[str],
        reason: str,
        output: str,
    ) -> InstalledReleaseError:
        """Build an error for a failed PTY-backed interactive check."""
        return cls(f"Installed interactive verification {reason}: {command}\n{output}")

    @classmethod
    def invalid_first_launch_defaults(cls) -> InstalledReleaseError:
        """Build an error for malformed installed setup defaults."""
        return cls("Installed first-launch defaults are not valid JSON")


def verify_installed_release() -> None:
    """Exercise the installed framework, SDK, resources, and native TUI."""
    _verify_isolated_interpreter()
    _verify_framework_imports()
    verify_console_entry_point()
    verify_first_launch_defaults()
    _verify_resources()
    _verify_materialized_sdk()
    _verify_tui()


def _verify_isolated_interpreter() -> None:
    if os.environ.get("PYTHONNOUSERSITE") != "1" or site.ENABLE_USER_SITE:
        _fail("User-site packages are not disabled")
    if os.environ.get("PYTHONPATH"):
        _fail("PYTHONPATH must be empty")
    _verify_required_system_tools()
    for executable in SYSTEM_JAVASCRIPT_TOOLS:
        if shutil.which(executable) is not None:
            _fail(f"System JavaScript runtime is present on PATH: {executable}")
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
    _run([executable, "--headless", "--help"], timeout=30)
    issue_mcp = shutil.which("vibesys-issue-mcp")
    if issue_mcp is None:
        _fail("Installed vibesys-issue-mcp console script is absent from PATH")
    _run([issue_mcp, "--help"], timeout=30)


def verify_first_launch_defaults() -> None:
    """Verify optional launch-directory config and no bundled default config."""
    distribution_files = importlib.metadata.distribution("vibesys").files or ()
    if any(Path(str(path)).name == "agent.toml" for path in distribution_files):
        _fail("Installed VibeSys distribution unexpectedly contains agent.toml")
    executable = shutil.which("vibesys")
    if executable is None:
        _fail("Installed vibesys console script is absent from PATH")
    config_path = _RUNTIME_ROOT / "agent.toml"
    if config_path.exists():
        _fail(f"Installed first launch unexpectedly contains a config file: {config_path}")
    config_path.write_text(
        """\
[model]
name = "gpt-5.4"

[repository]
visibility = "public"

[tui]
theme = "solarized-light"
"""
    )
    try:
        output = _run_capture(
            [executable, "tui-defaults"],
            timeout=30,
        )
    finally:
        config_path.unlink()
    defaults = _parse_first_launch_defaults(output, source="launch-directory")
    runs_dir = defaults.get("runs_dir")
    expected_runs_dir = (_RUNTIME_ROOT / "exp_env").resolve()
    if not isinstance(runs_dir, str) or Path(runs_dir).resolve() != expected_runs_dir:
        _fail(f"Installed first-launch runs directory is invalid: {runs_dir!r}")
    if defaults.get("input_path") != "":
        _fail(f"Installed first-launch input path must be empty: {defaults.get('input_path')!r}")
    expected_defaults = {
        "repository_owner": None,
        "visibility": "public",
        "theme": "solarized-light",
    }
    for key, expected in expected_defaults.items():
        if defaults.get(key) != expected:
            _fail(
                f"Installed first launch ignored agent.toml from its working directory: "
                f"{key}={defaults.get(key)!r}"
            )


def _parse_first_launch_defaults(output: str, *, source: str) -> dict[str, object]:
    try:
        defaults: object = json.loads(output)
    except json.JSONDecodeError as exc:
        raise InstalledReleaseError.invalid_first_launch_defaults() from exc
    if not isinstance(defaults, dict):
        _fail(f"Installed {source} defaults must be a JSON object")
    return cast("dict[str, object]", defaults)


def _verify_resources() -> None:
    root = resources_root()
    if root is None or "site-packages" not in str(root.resolve()):
        _fail(f"Installed resources did not resolve from site-packages: {root}")
    skill_roots = default_skill_roots()
    if len(skill_roots) != 1 or not any(skill_roots[0].rglob("SKILL.md")):
        _fail(f"Installed skills did not resolve: {skill_roots}")
    for kind in ACTIVE_PROFILER_KINDS:
        support = profiler_support_dir(kind.value)
        if support is None or "site-packages" not in str(support.resolve()):
            _fail(f"Installed profiler resources did not resolve for {kind.value}: {support}")
    for name in ("vibesys-evaluator-microservice", "vibesys-evaluator-queue"):
        package = resolve_evaluator_package(EvaluatorPackageRequirement(name=name, version="0.1.0"))
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
        dependencies = materialize_input_project(
            input_project,
            workspace,
            project_root=project_root,
            copy_dir=_copy_tree,
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


def _verify_tui() -> None:
    bundle = bundled_tui()
    if bundle is None:
        _fail("Installed release has no bundled TUI")
    if "site-packages" not in str(bundle.root.resolve()):
        _fail(f"Bundled TUI did not resolve from site-packages: {bundle.root}")
    environment = {
        **os.environ,
        "BUN_CONFIG_SKIP_INSTALL_PACKAGES": "1",
        "VIBESYS_PYTHON": sys.executable,
        "VIBESYS_TUI_RUNTIME": str(bundle.runtime),
    }
    _run(
        [str(bundle.runtime), str(bundle.root / "app" / "dist" / "self-test.js")],
        env=environment,
        timeout=30,
    )
    executable = shutil.which("vibesys")
    if executable is None:
        _fail("Installed vibesys console script is absent from PATH")
    run_interactive_tui_smoke(
        [executable],
        env=environment,
        runtime_root=_RUNTIME_ROOT,
        timeout=60,
    )
    run_headless_stub_smoke(
        [executable],
        env=environment,
        runtime_root=_RUNTIME_ROOT,
        timeout=60,
    )


def _write_stub_input(input_root: Path) -> None:
    input_root.mkdir()
    (input_root / "OBJECTIVE.md").write_text("Verify the installed release.\n")
    (input_root / "candidate.py").write_text("VALUE = 1\n")
    (input_root / "vibesys.input.toml").write_text(
        "version = 1\n"
        "\n"
        "[agent]\n"
        'domain = "generic"\n'
        "\n"
        "[accuracy]\n"
        'command = ["python", "-c", "raise SystemExit(0)"]\n'
        "\n"
        "[benchmark]\n"
        'command = ["python", "-c", "raise SystemExit(0)"]\n'
    )


def _copied_project_stub_smoke_command(
    command_prefix: list[str],
    *,
    input_root: Path,
    runs_root: Path,
    headless: bool,
) -> list[str]:
    return [
        *command_prefix,
        *(["--headless"] if headless else []),
        "--stub-agent",
        "--input",
        str(input_root),
        "--exp-name",
        "installed-release-smoke",
        "--max-rounds",
        "1",
        "--local",
        "--no-skills",
        "--backend",
        "cpu",
        "--profiler",
        "none",
        "--runs-dir",
        str(runs_root),
    ]


def _direct_project_stub_smoke_command(command_prefix: list[str]) -> list[str]:
    return [
        *command_prefix,
        "--headless",
        "--stub-agent",
        "--agent-backend",
        "cli",
        "--exp-name",
        "installed-release-smoke",
        "--max-rounds",
        "1",
        "--no-skills",
        "--backend",
        "cpu",
        "--profiler",
        "none",
    ]


def run_interactive_tui_smoke(
    command_prefix: list[str],
    *,
    env: dict[str, str],
    runtime_root: Path,
    timeout: int,
) -> None:
    """Run the installed interactive CLI through a PTY until controller startup."""
    with tempfile.TemporaryDirectory(prefix="vibesys-tui-smoke-", dir=runtime_root) as temporary:
        smoke_root = Path(temporary)
        input_root = smoke_root / "input"
        _write_stub_input(input_root)
        marker = smoke_root / "controller-started"
        runs_root = smoke_root / "runs"
        smoke_environment = {**env, TUI_SMOKE_MARKER_ENV: str(marker)}
        command = _copied_project_stub_smoke_command(
            command_prefix,
            input_root=input_root,
            runs_root=runs_root,
            headless=False,
        )
        _run_in_pty(command, env=smoke_environment, timeout=timeout)
        if not marker.is_file() or marker.read_text() != TUI_SMOKE_MARKER_CONTENT:
            _fail("Interactive TUI did not write its control-protocol marker")


def run_headless_stub_smoke(
    command_prefix: list[str],
    *,
    env: dict[str, str],
    runtime_root: Path,
    timeout: int,
) -> None:
    """Run a configless installed loop from a complete project working directory."""
    mutable_prefix_paths_before = _mutable_install_paths(Path(sys.prefix))
    with tempfile.TemporaryDirectory(
        prefix="vibesys-headless-smoke-", dir=runtime_root
    ) as temporary:
        smoke_root = Path(temporary)
        project_root = smoke_root / "project"
        _write_stub_input(project_root)
        command = _direct_project_stub_smoke_command(command_prefix)
        _run(command, env=env, cwd=project_root, timeout=timeout)
        _verify_project_state(project_root)
    added_prefix_paths = _mutable_install_paths(Path(sys.prefix)) - mutable_prefix_paths_before
    if added_prefix_paths:
        _fail(
            "Headless smoke created a mutable run tree or cache beneath the Python "
            f"installation prefix: {sorted(added_prefix_paths)}"
        )


def _verify_project_state(project_root: Path) -> None:
    if (project_root / "agent.toml").exists():
        _fail("Configless headless smoke unexpectedly created agent.toml")
    try:
        store = Project.open(project_root).state
        store.load_project()
        runs = store.list_runs()
    except ProjectError as exc:
        _fail(f"Project smoke did not create valid project state: {exc}")
    if len(runs) != 1 or not runs[0].run_id.endswith("-installed-release-smoke"):
        _fail(f"Project smoke did not create exactly one run: {runs}")
    run = runs[0]
    agent_state = AgentRunStateStore(store.portable_namespace(run.run_id, "agent")).load()
    completed_rounds = agent_state.rounds
    if len(completed_rounds) != 1 or completed_rounds[0].round_number != 1:
        _fail("Project smoke did not persist exactly one completed round")
    if store.current_run_id() != run.run_id:
        _fail("Project smoke did not persist its local current-run pointer")
    if not store.log_directory(run.run_id).is_dir():
        _fail("Project smoke did not create its machine-local log directory")
    if not (project_root / ".git").is_dir():
        _fail("Project smoke did not initialize Git in the project directory")


def _mutable_install_paths(prefix: Path) -> set[Path]:
    if not prefix.is_dir():
        return set()
    mutable_paths: set[Path] = set()
    for path in prefix.rglob("*"):
        parts = path.relative_to(prefix).parts
        if (
            "exp_env" in parts
            or ".hf_cache" in parts
            or any(
                parts[index : index + 2] == (".cache", "huggingface")
                for index in range(len(parts) - 1)
            )
        ):
            mutable_paths.add(path.resolve())
    return mutable_paths


def _run_in_pty(command: list[str], *, env: dict[str, str], timeout: int) -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except OSError as exc:
        os.close(master_fd)
        raise InstalledReleaseError.command_failed(command) from exc
    finally:
        os.close(slave_fd)

    output = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            readable, _, _ = select.select([master_fd], [], [], min(0.1, remaining))
            if readable:
                _read_pty_output(master_fd, output)
        while not timed_out and select.select([master_fd], [], [], 0)[0]:
            if not _read_pty_output(master_fd, output):
                break
    finally:
        os.close(master_fd)

    if timed_out:
        _terminate_process_group(process)
        detail = _render_pty_output(output)
        raise InstalledReleaseError.interactive_failed(
            command, f"timed out after {timeout}s", detail
        )

    return_code = process.wait()
    if return_code != 0:
        detail = _render_pty_output(output)
        raise InstalledReleaseError.interactive_failed(
            command, f"exited with status {return_code}", detail
        )


def _read_pty_output(master_fd: int, output: bytearray) -> bool:
    try:
        chunk = os.read(master_fd, 4096)
    except OSError as exc:
        if exc.errno == errno.EIO:
            return False
        raise
    if not chunk:
        return False
    output.extend(chunk)
    if len(output) > PTY_OUTPUT_LIMIT:
        del output[:-PTY_OUTPUT_LIMIT]
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    else:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5)


def _render_pty_output(output: bytearray) -> str:
    return bytes(output).decode(errors="replace").strip()


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int,
) -> None:
    try:
        subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=env,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise InstalledReleaseError.command_failed(command) from exc


def _run_capture(command: list[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise InstalledReleaseError.command_failed(command) from exc
    return result.stdout


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
