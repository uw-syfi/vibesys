from __future__ import annotations

import base64
import errno
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call

import pytest

from vibesys.sandbox import modal_evaluator

if TYPE_CHECKING:
    from pathlib import Path

_DEPLOY_STDOUT = (
    "Web Function URL: https://workspace--candidate.modal.run\n"
    "View Deployment: https://modal.com/apps/workspace/main/deployed/candidate-app\n"
)


@pytest.fixture(autouse=True)
def isolate_deployment_path_checks(request, monkeypatch) -> None:  # noqa: ANN001
    """Keep subprocess tests focused on orchestration, not the fake /workspace."""
    if request.node.name.startswith("test_deployment_path_"):
        return
    monkeypatch.setattr(
        modal_evaluator,
        "_deployment_path",
        lambda workspace, entrypoint: modal_evaluator.Path(workspace) / entrypoint,
    )


def test_deployment_path_accepts_project_file_and_contained_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    service = workspace / "deploy" / "service.py"
    service.parent.mkdir(parents=True)
    service.write_text("app = object()\n")
    alias = workspace / "service.py"
    alias.symlink_to(service.relative_to(workspace))

    assert (
        modal_evaluator._deployment_path(  # noqa: SLF001
            str(workspace), "deploy/service.py"
        )
        == service
    )
    assert (
        modal_evaluator._deployment_path(  # noqa: SLF001
            str(workspace), "service.py"
        )
        == service
    )


@pytest.mark.parametrize(
    "entrypoint",
    ["", ".", "../service.py", "/tmp/service.py"],  # noqa: S108
)
def test_deployment_path_rejects_non_relative_paths(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="project-relative"):
        modal_evaluator._deployment_path(str(workspace), entrypoint)  # noqa: SLF001


def test_deployment_path_rejects_missing_directory_and_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = workspace / "deploy"
    directory.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("app = object()\n")
    (workspace / "escape.py").symlink_to(outside)

    with pytest.raises(FileNotFoundError):
        modal_evaluator._deployment_path(str(workspace), "missing.py")  # noqa: SLF001
    with pytest.raises(ValueError, match="not a file"):
        modal_evaluator._deployment_path(str(workspace), "deploy")  # noqa: SLF001
    with pytest.raises(ValueError, match="escapes the project"):
        modal_evaluator._deployment_path(str(workspace), "escape.py")  # noqa: SLF001


def test_runtime_dir_is_per_user_without_xdg_runtime_dir(monkeypatch) -> None:  # noqa: ANN001
    """Falls back to a uid-suffixed temp directory when XDG_RUNTIME_DIR is unset."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(modal_evaluator.os, "getuid", lambda: 1001)
    first = modal_evaluator._runtime_dir()  # noqa: SLF001
    monkeypatch.setattr(modal_evaluator.os, "getuid", lambda: 1002)
    second = modal_evaluator._runtime_dir()  # noqa: SLF001

    assert first != second
    assert first.name == "vibesys-1001"
    assert second.name == "vibesys-1002"


def test_runtime_dir_prefers_xdg_runtime_dir(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Uses XDG_RUNTIME_DIR when set instead of the uid-suffixed temp fallback."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    assert modal_evaluator._runtime_dir() == tmp_path / "vibesys"  # noqa: SLF001


def test_import_does_not_validate_or_create_the_runtime_directory(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Importing only builds runtime paths, even when XDG points at a symlink."""
    target = tmp_path / "target"
    target.mkdir()
    hostile_runtime_dir = tmp_path / "runtime"
    hostile_runtime_dir.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(hostile_runtime_dir))
    calls: list[str] = []

    def record_call(*_args: object, **_kwargs: object) -> None:
        calls.append("filesystem")

    for method in ("mkdir", "chmod", "lstat"):
        monkeypatch.setattr(modal_evaluator.Path, method, record_call)
    module_name = "vibesys.sandbox._modal_evaluator_import_probe"
    spec = spec_from_file_location(module_name, modal_evaluator.__file__)
    if spec is None or spec.loader is None:
        raise AssertionError
    imported = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, imported)
    spec.loader.exec_module(imported)

    assert calls == []
    assert hostile_runtime_dir / "vibesys" / "modal-evaluator.lock" == imported._LOCK_PATH  # noqa: SLF001


def test_exclusive_evaluation_creates_private_runtime_dir_and_lock_file(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """The lock's runtime directory is created mode 0o700 before flock is taken."""
    lock_path = tmp_path / "rt" / "modal-evaluator.lock"
    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", lock_path)

    with modal_evaluator._exclusive_evaluation():  # noqa: SLF001
        pass

    runtime_dir = lock_path.parent
    assert runtime_dir.is_dir()
    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700
    assert lock_path.exists()


def test_ensure_runtime_dir_rejects_file_shadowing_the_directory(tmp_path: Path) -> None:
    """A plain file occupying the runtime-dir path raises a clear RuntimeError."""
    blocked = tmp_path / "rt"
    blocked.write_text("not a directory")
    target = blocked / "modal-evaluator.lock"

    with pytest.raises(RuntimeError, match="cannot use"):
        modal_evaluator._ensure_runtime_dir(target)  # noqa: SLF001


def test_ensure_runtime_dir_rejects_symlink_shadowing_the_directory(tmp_path: Path) -> None:
    """A symlink at the runtime-dir path is rejected instead of followed."""
    target_directory = tmp_path / "target"
    target_directory.mkdir()
    shadow = tmp_path / "rt"
    shadow.symlink_to(target_directory, target_is_directory=True)

    with pytest.raises(RuntimeError, match="expected a directory owned by uid"):
        modal_evaluator._ensure_runtime_dir(shadow / "modal-evaluator.lock")  # noqa: SLF001


def test_ensure_runtime_dir_rejects_pre_existing_world_writable_directory(
    tmp_path: Path,
) -> None:
    """A directory others could already have planted files in is rejected, not coerced."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o777)

    with pytest.raises(RuntimeError, match="group- or world-writable"):
        modal_evaluator._ensure_runtime_dir(runtime_dir / "modal-evaluator.lock")  # noqa: SLF001


def test_ensure_runtime_dir_accepts_pre_existing_readable_directory(tmp_path: Path) -> None:
    """0o755 is readable but not a planting vector, so it is tightened rather than rejected."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o755)

    modal_evaluator._ensure_runtime_dir(runtime_dir / "modal-evaluator.lock")  # noqa: SLF001

    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700


def test_exclusive_evaluation_rejects_symlink_planted_at_the_lock_path(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """A symlink at the lock path fails instead of truncating the file it points at."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    lock_path = runtime_dir / "modal-evaluator.lock"
    lock_path.symlink_to(outside)
    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", lock_path)

    with (
        pytest.raises(OSError, match=r"modal-evaluator\.lock") as raised,
        modal_evaluator._exclusive_evaluation(),  # noqa: SLF001
    ):
        pass

    assert raised.value.errno == errno.ELOOP
    assert outside.read_text() == "untouched"


def test_read_deployment_lease_rejects_symlink_planted_at_the_lease_path(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """A symlink at the lease path yields no lease instead of trusting its target."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"candidate_revision": "abc123", "base_url": "https://attacker.example"})
    )
    lease_path = runtime_dir / "modal-evaluator-deployment.json"
    lease_path.symlink_to(outside)
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)

    assert modal_evaluator._read_deployment_lease() is None  # noqa: SLF001


def test_write_deployment_lease_rejects_symlink_planted_at_the_staging_path(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """A symlink at the lease staging path fails instead of clobbering its target."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (runtime_dir / "modal-evaluator-deployment.tmp").symlink_to(outside)
    monkeypatch.setattr(
        modal_evaluator,
        "_DEPLOYMENT_LEASE_PATH",
        runtime_dir / "modal-evaluator-deployment.json",
    )

    with pytest.raises(OSError, match=r"modal-evaluator-deployment\.tmp") as raised:
        modal_evaluator._write_deployment_lease(  # noqa: SLF001
            "abc123", "https://example.invalid", None
        )

    assert raised.value.errno == errno.ELOOP
    assert outside.read_text() == "untouched"


def test_extract_modal_web_url_handles_rich_line_wrapping() -> None:
    output = """
    Created Web Function URL for Server.web_app =>
    │ https://workspace--vibesys-long-endpoint.moda
    │ l.run (label truncated)
    View Deployment: https://modal.com/apps/workspace/main/deployed/example
    """

    assert (
        modal_evaluator.extract_modal_web_url(output)
        == "https://workspace--vibesys-long-endpoint.modal.run"
    )


def test_extract_modal_web_url_handles_deploy_tree_wrapping() -> None:
    output = """
    ├── 🔨 Created web function fastapi_app =>
    │   https://vibeserve--vibesys-long-candidate-f51b76.moda
    │   l.run (label truncated)
    └── 🔨 Created function profile_remote.
    """

    assert (
        modal_evaluator.extract_modal_web_url(output)
        == "https://vibeserve--vibesys-long-candidate-f51b76.modal.run"
    )


def test_extract_modal_web_url_requires_endpoint() -> None:
    with pytest.raises(ValueError, match="did not print"):
        modal_evaluator.extract_modal_web_url("App deployed without a web function")


def test_extract_modal_app_identifier_handles_rich_line_wrapping() -> None:
    output = """
    View Deployment:
    │ https://modal.com/apps/workspace/main/deployed/vibesys-long-
    │ candidate
    """

    assert modal_evaluator.extract_modal_app_identifier(output) == "vibesys-long-candidate"


def test_setup_command_encoding_round_trips_opaque_argv() -> None:
    command = ("sh", "-c", "printf '%s' '$HOME; touch nope'", "")

    encoded = modal_evaluator.encode_setup_command(command)
    parsed = modal_evaluator._parser().parse_args(  # noqa: SLF001
        ["--setup-command-base64", encoded, "--", "true"]
    )

    assert parsed.setup_command_base64 == command
    assert parsed.command == ["--", "true"]


@pytest.mark.parametrize(
    "encoded",
    ["not-base64!", base64.urlsafe_b64encode(b"{}").decode()],
)
def test_setup_command_cli_rejects_malformed_payload(encoded: str) -> None:
    with pytest.raises(SystemExit):
        modal_evaluator._parser().parse_args(  # noqa: SLF001
            ["--setup-command-base64", encoded, "--", "true"]
        )


def test_plan_command_transfer_classifies_inputs_and_outputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task = workspace / ".vibesys" / "tasks" / "demo"
    bench = task / "benchmark"
    bench.mkdir(parents=True)
    (task / "requirements.txt").write_text("httpx\n")
    (bench / "benchmark.py").write_text("print('hi')\n")
    (workspace / "main.py").write_text("app = object()\n")
    outputs_parent = tmp_path / "outputs"
    outputs_parent.mkdir()

    plan = modal_evaluator._plan_command_transfer(  # noqa: SLF001
        [
            "uv",
            "run",
            "--no-project",
            "--with-requirements",
            ".vibesys/tasks/demo/requirements.txt",
            "python",
            ".vibesys/tasks/demo/benchmark/benchmark.py",
            "main.py",
            "--output-json",
            str(outputs_parent / "result.json"),
            str(tmp_path / "missing-dir" / "result.json"),
        ],
        str(workspace),
    )

    # benchmark/ is covered by the task directory staged for requirements.txt;
    # root-level files stage themselves rather than the whole workspace.
    assert plan.stage_paths == (".vibesys/tasks/demo", "main.py")
    assert plan.output_paths == (str(outputs_parent / "result.json"),)


def test_plan_command_transfer_ignores_escaping_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (workspace / "leak.txt").symlink_to(outside)

    plan = modal_evaluator._plan_command_transfer(  # noqa: SLF001
        ["python", "leak.txt"], str(workspace)
    )

    assert plan.stage_paths == ()


def test_plan_command_transfer_does_not_stage_candidate_root_for_trusted_go_package(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.go").write_text("package candidate\n")

    plan = modal_evaluator._plan_command_transfer(  # noqa: SLF001
        ["go", "-C", ".vibesys-evaluator-package", "run", "."],
        str(workspace),
    )

    assert plan.stage_paths == ()


@pytest.mark.parametrize(
    "command",
    [
        ["go", "run", ".", "-C", ".vibesys-evaluator-package"],
        ["go", "-C", ".vibesys-evaluator-package/..", "run", "."],
    ],
)
def test_plan_command_transfer_does_not_misclassify_untrusted_go_cwd(
    tmp_path: Path,
    command: list[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.go").write_text("package candidate\n")

    plan = modal_evaluator._plan_command_transfer(command, str(workspace))  # noqa: SLF001

    assert plan.stage_paths == (".",)


def test_build_stage_archive_preserves_relative_layout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "task" / "data"
    nested.mkdir(parents=True)
    (nested / "cases.json").write_text("[]")

    payload = modal_evaluator._build_stage_archive(str(workspace), ["task"])  # noqa: SLF001

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert "task/data/cases.json" in archive.getnames()


def test_build_stage_archive_places_trusted_package_at_reserved_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    (package / "adapter.py").write_text("print('ok')\n")

    payload = modal_evaluator._build_stage_archive(  # noqa: SLF001
        str(workspace),
        [],
        evaluator_package_root=str(package),
    )

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert ".vibesys-evaluator-package/adapter.py" in archive.getnames()


def test_build_stage_archive_stages_workspace_root_without_candidate_framework_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "go.mod").write_text("module example\n")
    for reserved in (
        ".vibesys-evaluator-package",
        ".vibesys-evaluator-tools",
        ".vibesys-evaluator-toolchains",
        ".bin",
        ".pip",
        ".uv-cache",
    ):
        (workspace / reserved).mkdir()
        (workspace / reserved / "poisoned").write_text("candidate")

    payload = modal_evaluator._build_stage_archive(  # noqa: SLF001
        str(workspace),
        ["."],
    )

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = archive.getnames()
        assert "go.mod" in names
        assert not any(name.startswith(".vibesys-evaluator-") for name in names)


@pytest.mark.parametrize(
    "reserved",
    [
        ".vibesys-evaluator-package",
        ".vibesys-evaluator-tools/nested/bin",
        ".vibesys-evaluator-toolchains/rustup",
        ".bin/go",
        ".pip/uv",
        ".uv-cache/archive",
    ],
)
def test_build_stage_archive_rejects_workspace_collision_with_framework_paths(
    tmp_path: Path,
    reserved: str,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / reserved).mkdir(parents=True)
    package = tmp_path / "package"
    package.mkdir()

    with pytest.raises(ValueError, match="collides with a reserved framework path"):
        modal_evaluator._build_stage_archive(  # noqa: SLF001
            str(workspace),
            [reserved],
            evaluator_package_root=str(package),
        )


def test_build_stage_archive_rejects_oversized_inputs(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "blob.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(modal_evaluator, "_MAX_STAGE_ARCHIVE_BYTES", 16)

    with pytest.raises(ValueError, match="too large"):
        modal_evaluator._build_stage_archive(str(workspace), ["blob.bin"])  # noqa: SLF001


def test_find_app_container_matches_snake_case_listing(monkeypatch) -> None:  # noqa: ANN001
    listing = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            [
                {"container_id": "ta-other", "app_name": "other-app"},
                {"container_id": "ta-123", "app_name": "candidate-app"},
            ]
        ),
        stderr="",
    )
    run = MagicMock(return_value=listing)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)

    container = modal_evaluator._find_app_container(  # noqa: SLF001
        "candidate-app",
        workspace="/workspace",
        base_url="https://workspace--candidate.modal.run",
    )

    assert container == "ta-123"
    assert run.call_args_list[0].args[0][:5] == ["uv", "run", "modal", "container", "list"]


def test_find_app_container_matches_title_case_listing(monkeypatch) -> None:  # noqa: ANN001
    listing = SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Container ID": "ta-123", "App Name": "candidate-app"}]),
        stderr="",
    )
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(return_value=listing))

    container = modal_evaluator._find_app_container(  # noqa: SLF001
        "candidate-app",
        workspace="/workspace",
        base_url="https://workspace--candidate.modal.run",
    )

    assert container == "ta-123"


def test_find_app_container_surfaces_cli_error(monkeypatch) -> None:  # noqa: ANN001
    listing = SimpleNamespace(returncode=2, stdout="", stderr="token expired")
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(return_value=listing))
    monkeypatch.setattr(modal_evaluator, "_healthy_now", MagicMock(return_value=True))
    monkeypatch.setattr(modal_evaluator.time, "sleep", lambda _: None)
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(modal_evaluator.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="container list exited 2: token expired"):
        modal_evaluator._find_app_container(  # noqa: SLF001
            "candidate-app",
            workspace="/workspace",
            base_url="https://workspace--candidate.modal.run",
            timeout_seconds=1.5,
        )


def test_find_app_container_rewarms_then_times_out(monkeypatch) -> None:  # noqa: ANN001
    listing = SimpleNamespace(returncode=0, stdout="[]", stderr="")
    run = MagicMock(return_value=listing)
    warm = MagicMock(return_value=True)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "_healthy_now", warm)
    monkeypatch.setattr(modal_evaluator.time, "sleep", lambda _: None)
    clock = iter([0.0, 0.0, 1.0, 2.0])
    monkeypatch.setattr(modal_evaluator.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="no running container"):
        modal_evaluator._find_app_container(  # noqa: SLF001
            "candidate-app",
            workspace="/workspace",
            base_url="https://workspace--candidate.modal.run",
            timeout_seconds=1.5,
        )
    assert warm.call_count == 2


def test_parse_exec_output_extracts_rc_files_and_passthrough() -> None:
    encoded = base64.b64encode(b'{"metric": 1}').decode()
    stdout = (
        "benchmark progress line\n"
        f"{modal_evaluator._OUTPUT_FILE_MARKER} /tmp/result.json\n"  # noqa: SLF001
        f"{encoded}\n"
        f"{modal_evaluator._OUTPUT_END_MARKER}\n"  # noqa: SLF001
        f"{modal_evaluator._EXEC_RC_MARKER}0\n"  # noqa: SLF001
    )

    rc, files, passthrough = modal_evaluator._parse_exec_output(stdout)  # noqa: SLF001

    assert rc == 0
    assert files == {"/tmp/result.json": b'{"metric": 1}'}  # noqa: S108
    assert passthrough == "benchmark progress line"


def test_parse_exec_output_without_rc_marker_returns_none() -> None:
    rc, files, passthrough = modal_evaluator._parse_exec_output("crashed early\n")  # noqa: SLF001

    assert rc is None
    assert files == {}
    assert passthrough == "crashed early"


def test_bootstrap_script_runs_command_verbatim_and_relays_outputs(tmp_path: Path) -> None:
    script = modal_evaluator._bootstrap_script(  # noqa: SLF001
        ["uv", "run", "python", "bench.py"],
        ["/tmp/result.json"],  # noqa: S108
    )

    assert "uv run python bench.py" in script
    assert "/tmp/result.json" in script  # noqa: S108
    assert modal_evaluator._EXEC_RC_MARKER in script  # noqa: SLF001
    assert "--url" not in script
    assert "setup_rc" not in script
    assert 'rm -rf "$stage/.bin" "$stage/.pip" "$stage/.uv-cache"' in script
    assert "python3 -I -m pip" in script
    wrapper_line = next(
        line for line in script.splitlines() if "base64 -d" in line and ".bin/uv" in line
    )
    encoded_wrapper = shlex.split(wrapper_line)[2]
    wrapper = base64.b64decode(encoded_wrapper).decode()
    assert "python3 -I -c" in wrapper
    stage = tmp_path / "stage"
    (stage / ".bin").mkdir(parents=True)
    (stage / ".pip" / "uv").mkdir(parents=True)
    (stage / ".pip" / "uv" / "__init__.py").write_text("")
    (stage / ".pip" / "uv" / "__main__.py").write_text("print('trusted uv')\n")
    wrapper_path = stage / ".bin" / "uv"
    wrapper_path.write_text(wrapper)
    wrapper_path.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "uv.py").write_text("print('candidate uv')\n")

    result = subprocess.run(  # noqa: S603
        [str(wrapper_path), "--help"],
        cwd=candidate,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trusted uv"


def test_bootstrap_script_runs_setup_before_evaluator_and_quotes_argv() -> None:
    script = modal_evaluator._bootstrap_script(  # noqa: SLF001
        ["python", "bench.py"],
        [],
        setup_command=["installer", "argument with spaces", "; touch nope"],
    )

    setup = "installer 'argument with spaces' '; touch nope'"
    assert setup in script
    assert script.index(setup) < script.index("python bench.py")
    assert script.index("export RUSTUP_HOME CARGO_HOME") < script.index("python bench.py")
    assert script.index("GOWORK=off") < script.index("python bench.py")
    assert "setup_rc=$?" in script
    assert str(modal_evaluator._TRUSTED_SETUP_FAILURE_EXIT_CODE) in script  # noqa: SLF001


def test_bootstrap_script_maps_setup_failure_to_distinct_exit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    evaluator_ran = tmp_path / "evaluator-ran"
    archive = modal_evaluator._build_stage_archive(str(workspace), [])  # noqa: SLF001
    encoded = base64.b64encode(archive).decode("ascii")
    script = modal_evaluator._bootstrap_script(  # noqa: SLF001
        ["sh", "-c", f"touch {evaluator_ran}"],
        [],
        setup_command=["sh", "-c", "exit 23"],
    )

    result = subprocess.run(  # noqa: S603
        ["sh", "-c", script, "vibesys-eval", encoded],  # noqa: S607
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        text=True,
    )
    rc, _, _ = modal_evaluator._parse_exec_output(result.stdout)  # noqa: SLF001

    assert result.returncode == 0
    assert rc == modal_evaluator._TRUSTED_SETUP_FAILURE_EXIT_CODE  # noqa: SLF001
    assert "setup failed (exit 23)" in result.stderr
    assert not evaluator_ran.exists()


def _colocated_exec_result(rc: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=f"{modal_evaluator._EXEC_RC_MARKER}{rc}\n",  # noqa: SLF001
        stderr="",
    )


def test_execute_colocated_execs_in_container_and_returns_sentinel_rc(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "checker.py").write_text("print('ok')\n")
    run = MagicMock(return_value=_colocated_exec_result(rc=3))
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "_find_app_container", MagicMock(return_value="ta-123"))
    keepwarm = MagicMock()
    keepwarm.return_value.__enter__ = MagicMock()
    keepwarm.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(modal_evaluator, "_DeploymentKeepWarm", keepwarm)

    result = modal_evaluator._execute_colocated(  # noqa: SLF001
        ["python", "checker.py"],
        workspace=str(workspace),
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
    )

    assert result == 3
    keepwarm.assert_called_once_with("https://workspace--candidate.modal.run")
    exec_argv = run.call_args.args[0]
    assert exec_argv[:6] == ["uv", "run", "modal", "container", "exec", "ta-123"]
    assert exec_argv[6:9] == ["--", "sh", "-c"]
    assert "python checker.py" in exec_argv[9]


def test_execute_colocated_stages_package_and_includes_setup(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    (package / "adapter.py").write_text("print('ok')\n")
    run = MagicMock(return_value=_colocated_exec_result())
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "_find_app_container", MagicMock(return_value="ta-123"))

    result = modal_evaluator._execute_colocated(  # noqa: SLF001
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace=str(workspace),
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
        setup_command=["sh", "-c", "echo prepared"],
        evaluator_package_root=str(package),
    )

    assert result == 0
    exec_argv = run.call_args.args[0]
    assert "sh -c 'echo prepared'" in exec_argv[9]
    payload = base64.b64decode("".join(exec_argv[11:]))
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert ".vibesys-evaluator-package/adapter.py" in archive.getnames()


def test_execute_colocated_writes_relayed_output_files(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_path = tmp_path / "result.json"
    encoded = base64.b64encode(b'{"metric": 2}').decode()
    exec_result = SimpleNamespace(
        returncode=0,
        stdout=(
            f"{modal_evaluator._OUTPUT_FILE_MARKER} {output_path}\n"  # noqa: SLF001
            f"{encoded}\n"
            f"{modal_evaluator._OUTPUT_END_MARKER}\n"  # noqa: SLF001
            f"{modal_evaluator._EXEC_RC_MARKER}0\n"  # noqa: SLF001
        ),
        stderr="",
    )
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(return_value=exec_result))
    monkeypatch.setattr(modal_evaluator, "_find_app_container", MagicMock(return_value="ta-123"))

    result = modal_evaluator._execute_colocated(  # noqa: SLF001
        ["python", "bench.py", "--output-json", str(output_path)],
        workspace=str(workspace),
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
    )

    assert result == 0
    assert json.loads(output_path.read_text()) == {"metric": 2}


def test_execute_colocated_reports_missing_rc_sentinel(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
    capsys,  # noqa: ANN001
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exec_result = SimpleNamespace(returncode=0, stdout="killed mid-run\n", stderr="")
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(return_value=exec_result))
    monkeypatch.setattr(modal_evaluator, "_find_app_container", MagicMock(return_value="ta-123"))

    result = modal_evaluator._execute_colocated(  # noqa: SLF001
        ["python", "bench.py"],
        workspace=str(workspace),
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
    )

    assert result == 1
    assert "did not report an exit code" in capsys.readouterr().err


def test_run_evaluator_deploys_waits_and_runs_colocated(monkeypatch) -> None:  # noqa: ANN001
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(return_value=deploy)
    wait = MagicMock()
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", wait)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert run.call_args_list == [
        call(
            ["uv", "run", "modal", "deploy", "/workspace/main.py"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            check=False,
        ),
    ]
    wait.assert_called_once_with(
        "https://workspace--candidate.modal.run",
        timeout_seconds=90,
    )
    colocated.assert_called_once_with(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
        setup_command=None,
        evaluator_package_root=None,
    )


def test_run_evaluator_threads_trusted_setup_and_package(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(return_value=deploy))
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace="/workspace",
        setup_command=["installer", "--locked"],
        evaluator_package_root=str(package),
    )

    assert result == 0
    colocated.assert_called_once_with(
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace="/workspace",
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
        setup_command=("installer", "--locked"),
        evaluator_package_root=str(package.resolve()),
    )


def test_main_decodes_setup_command_and_forwards_package(monkeypatch) -> None:  # noqa: ANN001
    run = MagicMock(return_value=4)
    monkeypatch.setattr(modal_evaluator, "run_evaluator", run)
    encoded = modal_evaluator.encode_setup_command(["installer", "argument with spaces"])

    result = modal_evaluator.main(
        [
            "--workspace",
            "/workspace",
            "--setup-command-base64",
            encoded,
            "--evaluator-package-root",
            "/opt/vibesys-evaluator-package",
            "--",
            "python",
            ".vibesys-evaluator-package/adapter.py",
        ]
    )

    assert result == 4
    run.assert_called_once_with(
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace="/workspace",
        entrypoint="main.py",
        readiness_timeout_seconds=90,
        setup_command=("installer", "argument with spaces"),
        evaluator_package_root="/opt/vibesys-evaluator-package",
    )


def test_run_evaluator_requires_app_identifier(monkeypatch, capsys) -> None:  # noqa: ANN001
    deploy = SimpleNamespace(
        returncode=0,
        stdout="Web Function URL: https://workspace--candidate.modal.run\n",
        stderr="",
    )
    run = MagicMock(return_value=deploy)
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 1
    colocated.assert_not_called()
    assert "did not print a deployment URL" in capsys.readouterr().err


def test_run_evaluator_deploys_custom_entrypoint(monkeypatch) -> None:  # noqa: ANN001
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(return_value=deploy)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["checker"],
        workspace="/workspace",
        entrypoint="examples/deployment/service.py",
    )

    assert result == 0
    assert run.call_args_list[0] == call(
        [
            "uv",
            "run",
            "modal",
            "deploy",
            "/workspace/examples/deployment/service.py",
        ],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_evaluator_reuses_healthy_deployment_for_exact_revision(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
) -> None:
    lease_path = tmp_path / "deployment.json"
    lease_path.write_text(
        json.dumps(
            {
                "candidate_revision": "abc123",
                "base_url": "https://workspace--candidate.modal.run",
                "app_identifier": "candidate-app",
            }
        )
    )
    healthy = MagicMock(return_value=True)
    colocated = MagicMock(return_value=0)
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator, "_healthy_now", healthy)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock())

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    healthy.assert_called_once_with("https://workspace--candidate.modal.run")
    colocated.assert_called_once_with(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
        app_identifier="candidate-app",
        base_url="https://workspace--candidate.modal.run",
        setup_command=None,
        evaluator_package_root=None,
    )


def test_run_evaluator_redeploys_when_lease_lacks_app_identifier(
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    lease_path = tmp_path / "deployment.json"
    lease_path.write_text(
        json.dumps(
            {
                "candidate_revision": "abc123",
                "base_url": "https://workspace--candidate.modal.run",
            }
        )
    )
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(return_value=deploy)
    colocated = MagicMock(return_value=0)
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert run.call_args_list[0].args[0][:4] == ["uv", "run", "modal", "deploy"]
    colocated.assert_called_once()


def test_run_evaluator_releases_reused_deployment_after_final_gate(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
) -> None:
    lease_path = tmp_path / "deployment.json"
    lease_path.write_text(
        json.dumps(
            {
                "candidate_revision": "abc123",
                "base_url": "https://workspace--candidate.modal.run",
                "app_identifier": "candidate-app",
            }
        )
    )
    stop = SimpleNamespace(returncode=0, stdout="", stderr="")
    run = MagicMock(return_value=stop)
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setenv("VIBESYS_RELEASE_MODAL_DEPLOYMENT", "1")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator, "_healthy_now", MagicMock(return_value=True))
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert run.call_args_list[-1] == call(
        ["uv", "run", "modal", "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert not lease_path.exists()


def test_run_evaluator_releases_new_deployment_after_final_gate(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
) -> None:
    lease_path = tmp_path / "deployment.json"
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    stop = SimpleNamespace(returncode=0, stdout="", stderr="")
    run = MagicMock(side_effect=[deploy, stop])
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setenv("VIBESYS_RELEASE_MODAL_DEPLOYMENT", "1")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert run.call_args_list[-1] == call(
        ["uv", "run", "modal", "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert not lease_path.exists()


def test_run_evaluator_stops_mismatched_leased_app_before_redeploy(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
) -> None:
    lease_path = tmp_path / "deployment.json"
    lease_path.write_text(
        json.dumps(
            {
                "candidate_revision": "old",
                "base_url": "https://workspace--old.modal.run",
                "app_identifier": "old-app",
            }
        )
    )
    stop = SimpleNamespace(returncode=0, stdout="", stderr="")
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(side_effect=[stop, deploy])
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "new")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert run.call_args_list[0] == call(
        ["uv", "run", "modal", "app", "stop", "old-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_run_evaluator_redeploys_and_replaces_mismatched_revision(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
) -> None:
    lease_path = tmp_path / "deployment.json"
    lease_path.write_text(
        json.dumps(
            {
                "candidate_revision": "old",
                "base_url": "https://workspace--old.modal.run",
            }
        )
    )
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(return_value=deploy)
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "new")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", MagicMock())
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 0
    assert json.loads(lease_path.read_text()) == {
        "candidate_revision": "new",
        "base_url": "https://workspace--candidate.modal.run",
        "app_identifier": "candidate-app",
    }


def test_run_evaluator_prints_modal_logs_when_readiness_fails(
    monkeypatch,  # noqa: ANN001  # tracked: #288
    capsys,  # noqa: ANN001  # tracked: #288
) -> None:
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    run = MagicMock(return_value=deploy)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(
        modal_evaluator,
        "wait_for_health",
        MagicMock(side_effect=TimeoutError("not ready")),
    )
    logs = MagicMock(return_value="RuntimeError: CUDA toolkit mismatch")
    monkeypatch.setattr(modal_evaluator, "recent_modal_logs", logs)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
    )

    assert result == 1
    logs.assert_called_once_with("candidate-app", workspace="/workspace")
    assert "RuntimeError: CUDA toolkit mismatch" in capsys.readouterr().err
    assert (
        call(
            ["uv", "run", "modal", "app", "stop", "candidate-app", "--yes"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        in run.call_args_list
    )
