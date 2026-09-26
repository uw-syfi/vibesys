from __future__ import annotations

import base64
import errno
import io
import json
import os
import shlex
import stat
import sys
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, Unpack, cast
from unittest.mock import MagicMock, call

import pytest
from tests.support import run_test_command

from vibesys.sandbox import modal_evaluator
from vibesys.sandbox.modal_evaluator import (
    _MAX_DIAGNOSTIC_CHARS,
    _MAX_ENCODED_SETUP_COMMAND_CHARS,
    _RELEASE_DEPLOYMENT_ENV,
    _build_stage_archive,
    _decode_setup_command,
    _execute_colocated,
    _execute_reused_candidate,
    _healthy_now,
    _modal_health_url,
    _stop_modal_app,
)

_UV_EXECUTABLE = modal_evaluator.shutil.which("uv") or "uv"

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _EvaluatorOptions(TypedDict, total=False):
    evaluator_package_root: Path | None
    setup_command: list[str] | None
    container_listing: SimpleNamespace | None
    exec_stdout: str


_DEPLOY_STDOUT = (
    "Web Function URL: https://workspace--candidate.modal.run\n"
    "View Deployment: https://modal.com/apps/workspace/main/deployed/candidate-app\n"
)


@pytest.fixture(autouse=True)
def isolate_deployment_path_checks(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep subprocess tests focused on orchestration, not the fake /workspace."""
    if request.node.name.startswith(("test_deployment_path_", "test_lock_path_")):
        return
    monkeypatch.setattr(
        modal_evaluator,
        "_deployment_path",
        lambda workspace, entrypoint: modal_evaluator.Path(workspace) / entrypoint,
    )


def _run_deployment_path_case(
    workspace: Path,
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_revision: str | None = None,
) -> list[str]:
    deployed_paths: list[str] = []
    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", tmp_path / "runtime" / "lock")
    monkeypatch.setattr(
        modal_evaluator,
        "_DEPLOYMENT_LEASE_PATH",
        tmp_path / "runtime" / "modal-evaluator-deployment.json",
    )
    if candidate_revision is not None:
        monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", candidate_revision)

    def run_deploy(command: list[str], **_options: object) -> SimpleNamespace:
        deployed_paths.append(str(command[-1]))
        return SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")

    monkeypatch.setattr(modal_evaluator.subprocess, "run", run_deploy)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", lambda *_args, **_kwargs: 0)
    modal_evaluator.run_evaluator(["true"], workspace=str(workspace), entrypoint=entrypoint)
    return deployed_paths


def _run_with_lock_path(lock_path: Path, tmp_path: Path) -> int:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", lock_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    try:
        return modal_evaluator.run_evaluator(["true"], workspace=str(workspace))
    finally:
        monkeypatch.undo()


def _run_public_evaluator(
    workspace: Path,
    command: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **options: Unpack[_EvaluatorOptions],
) -> tuple[int, list[list[str]]]:
    evaluator_package_root = options.get("evaluator_package_root")
    setup_command = options.get("setup_command")
    container_listing = options.get("container_listing")
    exec_stdout = options.get("exec_stdout", "__VIBESYS_EXEC_RC__=0\n")
    calls: list[list[str]] = []

    def run(command: list[str], **_options: object) -> SimpleNamespace:
        calls.append(command)
        if command[3] == "deploy":
            return SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
        if command[3:5] == ["container", "list"]:
            return container_listing or SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"app_name": "candidate-app", "container_id": "ta-123"}]),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=exec_stdout,
            stderr="",
        )

    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", tmp_path / "runtime" / "lock")
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)
    monkeypatch.setattr(modal_evaluator, "wait_for_health", lambda *_args, **_kwargs: None)
    keepwarm = MagicMock()
    keepwarm.return_value.__enter__ = MagicMock()
    keepwarm.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(modal_evaluator, "_DeploymentKeepWarm", keepwarm)
    result = modal_evaluator.run_evaluator(
        command,
        workspace=str(workspace),
        evaluator_package_root=(
            str(evaluator_package_root) if evaluator_package_root is not None else None
        ),
        setup_command=setup_command,
    )
    return result, calls


def _staged_archive(calls: list[list[str]]) -> tarfile.TarFile:
    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    payload = base64.b64decode("".join(exec_command[11:]))
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")


def _load_modal_evaluator(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    module_name = f"vibesys.sandbox._modal_evaluator_{name}"
    spec = spec_from_file_location(module_name, modal_evaluator.__file__)
    if spec is None or spec.loader is None:
        raise AssertionError
    imported = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, imported)
    spec.loader.exec_module(imported)
    return imported


def test_deployment_path_accepts_project_file_and_contained_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    service = workspace / "deploy" / "service.py"
    service.parent.mkdir(parents=True)
    service.write_text("app = object()\n")
    alias = workspace / "service.py"
    alias.symlink_to(service.relative_to(workspace))

    assert _run_deployment_path_case(workspace, "deploy/service.py", tmp_path, monkeypatch) == [
        str(service)
    ]
    assert _run_deployment_path_case(workspace, "service.py", tmp_path, monkeypatch) == [
        str(service)
    ]


@pytest.mark.parametrize(
    "entrypoint",
    ["", ".", "../service.py"],
)
def test_deployment_path_rejects_non_relative_paths(
    tmp_path: Path,
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert _run_deployment_path_case(workspace, entrypoint, tmp_path, monkeypatch) == []


def test_deployment_path_rejects_missing_directory_and_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = workspace / "deploy"
    directory.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("app = object()\n")
    (workspace / "escape.py").symlink_to(outside)

    assert _run_deployment_path_case(workspace, "missing.py", tmp_path, monkeypatch) == []
    assert _run_deployment_path_case(workspace, "deploy", tmp_path, monkeypatch) == []
    assert _run_deployment_path_case(workspace, "escape.py", tmp_path, monkeypatch) == []


def test_deployment_path_rejects_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert (
        _run_deployment_path_case(workspace, str(tmp_path / "service.py"), tmp_path, monkeypatch)
        == []
    )


def test_runtime_dir_is_per_user_without_xdg_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public evaluation creates its lock under a uid-suffixed temp directory."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    real_uid = os.getuid()
    monkeypatch.setattr(modal_evaluator.os, "getuid", lambda: 1001)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _load_modal_evaluator(monkeypatch, "uid_1001")
    monkeypatch.setattr(first.os, "getuid", lambda: real_uid)
    assert first.run_evaluator(["true"], workspace=str(workspace)) == 1
    monkeypatch.setattr(modal_evaluator.os, "getuid", lambda: 1002)
    second = _load_modal_evaluator(monkeypatch, "uid_1002")
    monkeypatch.setattr(second.os, "getuid", lambda: real_uid)
    assert second.run_evaluator(["true"], workspace=str(workspace)) == 1

    first_lock = tmp_path / "vibesys-1001" / "modal-evaluator.lock"
    second_lock = tmp_path / "vibesys-1002" / "modal-evaluator.lock"
    assert first_lock.exists()
    assert second_lock.exists()


def test_runtime_dir_prefers_xdg_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public evaluation creates its lock under XDG_RUNTIME_DIR when configured."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    imported = _load_modal_evaluator(monkeypatch, "xdg")

    assert imported.run_evaluator(["true"], workspace=str(workspace)) == 1
    assert (tmp_path / "vibesys" / "modal-evaluator.lock").exists()


def test_import_does_not_validate_or_create_the_runtime_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert not (target / "vibesys").exists()


def test_lock_path_creates_private_runtime_dir_and_lock_file(tmp_path: Path) -> None:
    """The lock's runtime directory is created mode 0o700 before flock is taken."""
    lock_path = tmp_path / "rt" / "modal-evaluator.lock"

    assert _run_with_lock_path(lock_path, tmp_path) == 1

    runtime_dir = lock_path.parent
    assert runtime_dir.is_dir()
    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700
    assert lock_path.exists()


def test_lock_path_rejects_file_shadowing_the_directory(tmp_path: Path) -> None:
    """A plain file occupying the runtime-dir path raises a clear RuntimeError."""
    blocked = tmp_path / "rt"
    blocked.write_text("not a directory")
    target = blocked / "modal-evaluator.lock"

    with pytest.raises(RuntimeError, match="cannot use"):
        _run_with_lock_path(target, tmp_path)


def test_lock_path_rejects_symlink_shadowing_the_directory(tmp_path: Path) -> None:
    """A symlink at the runtime-dir path is rejected instead of followed."""
    target_directory = tmp_path / "target"
    target_directory.mkdir()
    shadow = tmp_path / "rt"
    shadow.symlink_to(target_directory, target_is_directory=True)

    with pytest.raises(RuntimeError, match="expected a directory owned by uid"):
        _run_with_lock_path(shadow / "modal-evaluator.lock", tmp_path)


def test_lock_path_rejects_pre_existing_world_writable_directory(
    tmp_path: Path,
) -> None:
    """A directory others could already have planted files in is rejected, not coerced."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o777)

    with pytest.raises(RuntimeError, match="group- or world-writable"):
        _run_with_lock_path(runtime_dir / "modal-evaluator.lock", tmp_path)


def test_lock_path_accepts_pre_existing_readable_directory(tmp_path: Path) -> None:
    """0o755 is readable but not a planting vector, so it is tightened rather than rejected."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o755)

    assert _run_with_lock_path(runtime_dir / "modal-evaluator.lock", tmp_path) == 1

    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o700


def test_lock_path_rejects_symlink_planted_at_the_lock_path(tmp_path: Path) -> None:
    """A symlink at the lock path fails instead of truncating the file it points at."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    lock_path = runtime_dir / "modal-evaluator.lock"
    lock_path.symlink_to(outside)
    with (
        pytest.raises(OSError, match=r"modal-evaluator\.lock") as raised,
    ):
        _run_with_lock_path(lock_path, tmp_path)

    assert raised.value.errno == errno.ELOOP
    assert outside.read_text() == "untouched"


def test_deployment_ignores_symlink_planted_at_the_lease_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink at the lease path yields no lease instead of trusting its target."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = workspace / "main.py"
    service.write_text("app = object()\n")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"candidate_revision": "abc123", "base_url": "https://attacker.example"})
    )
    lease_path = runtime_dir / "modal-evaluator-deployment.json"
    lease_path.symlink_to(outside)

    assert _run_deployment_path_case(
        workspace, "main.py", tmp_path, monkeypatch, candidate_revision="new-revision"
    ) == [str(service)]
    assert outside.read_text().startswith('{"candidate_revision": "abc123"')
    assert not lease_path.is_symlink()


def test_deployment_refuses_symlink_at_lease_staging_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink at the lease staging path fails instead of clobbering its target."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = workspace / "main.py"
    service.write_text("app = object()\n")
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (runtime_dir / "modal-evaluator-deployment.tmp").symlink_to(outside)

    with pytest.raises(OSError, match=r"modal-evaluator-deployment\.tmp") as raised:
        _run_deployment_path_case(
            workspace, "main.py", tmp_path, monkeypatch, candidate_revision="abc123"
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


def test_setup_command_encoding_round_trips_opaque_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ("sh", "-c", "printf '%s' '$HOME; touch nope'", "")

    encoded = modal_evaluator.encode_setup_command(command)
    parsed: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        modal_evaluator,
        "run_evaluator",
        lambda *args, **kwargs: parsed.append((args, kwargs)) or 0,
    )
    assert modal_evaluator.main(["--setup-command-base64", encoded, "--", "true"]) == 0

    assert parsed == [
        (
            (["true"],),
            {
                "workspace": "/workspace",
                "entrypoint": "main.py",
                "readiness_timeout_seconds": 90,
                "setup_command": command,
                "evaluator_package_root": None,
            },
        )
    ]


@pytest.mark.parametrize(
    "encoded",
    ["not-base64!", base64.urlsafe_b64encode(b"{}").decode()],
)
def test_setup_command_cli_rejects_malformed_payload(encoded: str) -> None:
    with pytest.raises(SystemExit):
        modal_evaluator.main(["--setup-command-base64", encoded, "--", "true"])


def test_evaluator_stages_inputs_and_relays_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    task = workspace / ".vibesys" / "tasks" / "demo"
    bench = task / "benchmark"
    bench.mkdir(parents=True)
    (task / "requirements.txt").write_text("httpx\n")
    (bench / "benchmark.py").write_text("print('hi')\n")
    (workspace / "main.py").write_text("app = object()\n")
    outputs_parent = tmp_path / "outputs"
    outputs_parent.mkdir()

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
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
        tmp_path,
        monkeypatch,
    )

    assert result == 0
    with _staged_archive(calls) as archive:
        names = archive.getnames()
    assert ".vibesys/tasks/demo/benchmark/benchmark.py" in names
    assert ".vibesys/tasks/demo/requirements.txt" in names
    assert "main.py" in names
    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    assert str(outputs_parent / "result.json") in exec_command[9]


def test_evaluator_does_not_stage_escaping_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (workspace / "leak.txt").symlink_to(outside)

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(workspace, ["python", "leak.txt"], tmp_path, monkeypatch)

    assert result == 0
    with _staged_archive(calls) as archive:
        assert archive.getnames() == []


def test_evaluator_does_not_stage_candidate_root_for_trusted_go_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.go").write_text("package candidate\n")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
        ["go", "-C", ".vibesys-evaluator-package", "run", "."],
        tmp_path,
        monkeypatch,
    )

    assert result == 0
    with _staged_archive(calls) as archive:
        assert archive.getnames() == []


@pytest.mark.parametrize(
    "command",
    [
        ["go", "run", ".", "-C", ".vibesys-evaluator-package"],
        ["go", "-C", ".vibesys-evaluator-package/..", "run", "."],
    ],
)
def test_evaluator_stages_untrusted_go_cwd(
    tmp_path: Path,
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.go").write_text("package candidate\n")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(workspace, command, tmp_path, monkeypatch)

    assert result == 0
    with _staged_archive(calls) as archive:
        assert "candidate.go" in archive.getnames()


def test_evaluator_preserves_staged_input_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "task" / "data"
    nested.mkdir(parents=True)
    (nested / "cases.json").write_text("[]")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace, ["python", "task/data/cases.json"], tmp_path, monkeypatch
    )

    assert result == 0
    with _staged_archive(calls) as archive:
        assert "task/data/cases.json" in archive.getnames()


def test_evaluator_stages_trusted_package_at_reserved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    (package / "adapter.py").write_text("print('ok')\n")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
        ["python", ".vibesys-evaluator-package/adapter.py"],
        tmp_path,
        monkeypatch,
        evaluator_package_root=package,
    )

    assert result == 0
    with _staged_archive(calls) as archive:
        assert ".vibesys-evaluator-package/adapter.py" in archive.getnames()


def test_evaluator_stages_workspace_root_without_candidate_framework_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    result, calls = _run_public_evaluator(workspace, ["python", "."], tmp_path, monkeypatch)

    assert result == 0
    with _staged_archive(calls) as archive:
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
def test_evaluator_rejects_workspace_collision_with_framework_paths(
    tmp_path: Path,
    reserved: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / reserved).mkdir(parents=True)
    (workspace / "main.py").write_text("app = object()\n")

    result, calls = _run_public_evaluator(workspace, ["python", reserved], tmp_path, monkeypatch)
    assert result == 1
    assert not any(call[3:5] == ["container", "exec"] for call in calls)


def test_evaluator_rejects_oversized_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "blob.bin").write_bytes(b"x" * 4096)
    monkeypatch.setattr(modal_evaluator, "_MAX_STAGE_ARCHIVE_BYTES", 16)
    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(workspace, ["python", "blob.bin"], tmp_path, monkeypatch)

    assert result == 1
    assert not any(call[3:5] == ["container", "exec"] for call in calls)


def test_evaluator_selects_matching_snake_case_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
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

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    assert result == 0
    assert exec_command[5] == "ta-123"


def test_evaluator_selects_matching_title_case_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Container ID": "ta-123", "App Name": "candidate-app"}]),
        stderr="",
    )

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    assert result == 0
    assert exec_command[5] == "ta-123"


def test_evaluator_reports_container_list_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = SimpleNamespace(returncode=2, stdout="", stderr="token expired")
    monkeypatch.setattr(modal_evaluator, "_healthy_now", MagicMock(return_value=True))
    monkeypatch.setattr(modal_evaluator.time, "sleep", lambda _: None)

    ticks = 0

    def monotonic() -> float:
        nonlocal ticks
        ticks += 1
        return 0.0 if ticks < 3 else 1_000.0

    monkeypatch.setattr(modal_evaluator.time, "monotonic", monotonic)

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )
    assert result == 1
    assert not any(call[3:5] == ["container", "exec"] for call in calls)
    assert "container list exited 2: token expired" in capsys.readouterr().err


def test_evaluator_reports_missing_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = SimpleNamespace(returncode=0, stdout="[]", stderr="")
    warm = MagicMock(return_value=True)
    monkeypatch.setattr(modal_evaluator, "_healthy_now", warm)
    monkeypatch.setattr(modal_evaluator.time, "sleep", lambda _: None)

    ticks = 0

    def monotonic() -> float:
        nonlocal ticks
        ticks += 1
        return 0.0 if ticks < 3 else 1_000.0

    monkeypatch.setattr(modal_evaluator.time, "monotonic", monotonic)
    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )
    assert result == 1
    assert warm.call_count == 1
    assert not any(call[3:5] == ["container", "exec"] for call in calls)
    assert "no running container found" in capsys.readouterr().err


def test_evaluator_relays_exec_outputs_and_passes_through_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    output_path = tmp_path / "result.json"
    encoded = base64.b64encode(b'{"metric": 1}').decode()
    stdout = (
        "benchmark progress line\n"
        f"__VIBESYS_OUTPUT_FILE__ {output_path}\n"
        f"{encoded}\n"
        "__VIBESYS_OUTPUT_END__\n"
        "__VIBESYS_EXEC_RC__=0\n"
    )

    result, _calls = _run_public_evaluator(
        workspace,
        ["python", "main.py", "--output-json", str(output_path)],
        tmp_path,
        monkeypatch,
        exec_stdout=stdout,
    )

    assert result == 0
    assert json.loads(output_path.read_text()) == {"metric": 1}
    assert capsys.readouterr().out == "benchmark progress line\n"


def test_evaluator_reports_missing_exec_status_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    result, _calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, exec_stdout="crashed early\n"
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "crashed early" in captured.err
    assert "did not report an exit code" in captured.err


def test_evaluator_bootstrap_runs_command_verbatim_and_relays_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    result_path = tmp_path / "result.json"
    result, calls = _run_public_evaluator(
        workspace,
        ["uv", "run", "python", "bench.py", "--output-json", str(result_path)],
        tmp_path,
        monkeypatch,
    )
    assert result == 0
    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    script = exec_command[9]

    assert "uv run python bench.py" in script
    assert str(result_path) in script
    assert "__VIBESYS_EXEC_RC__=" in script
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

    monkeypatch.undo()
    result = run_test_command(
        [str(wrapper_path), "--help"],
        cwd=candidate,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trusted uv"


def test_evaluator_bootstrap_runs_setup_before_evaluator_and_quotes_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
        ["python", "bench.py"],
        tmp_path,
        monkeypatch,
        setup_command=["installer", "argument with spaces", "; touch nope"],
    )
    assert result == 0
    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    script = exec_command[9]

    setup = "installer 'argument with spaces' '; touch nope'"
    assert setup in script
    assert script.index(setup) < script.index("python bench.py")
    assert script.index("export RUSTUP_HOME CARGO_HOME") < script.index("python bench.py")
    assert script.index("GOWORK=off") < script.index("python bench.py")
    assert "setup_rc=$?" in script
    assert " 96" in script


def test_bootstrap_script_maps_setup_failure_to_distinct_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)
    evaluator_ran = tmp_path / "evaluator-ran"
    (workspace / "main.py").write_text("app = object()\n")
    result_code, calls = _run_public_evaluator(
        workspace,
        ["sh", "-c", f"touch {evaluator_ran}"],
        tmp_path,
        monkeypatch,
        setup_command=["sh", "-c", "exit 23"],
    )

    assert result_code == 0
    exec_command = next(call for call in calls if call[3:5] == ["container", "exec"])
    script = exec_command[9]
    encoded = "".join(exec_command[11:])
    monkeypatch.undo()
    result = run_test_command(
        ["sh", "-c", script, "vibesys-eval", encoded],
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        text=True,
    )
    assert result.returncode == 0
    assert "__VIBESYS_EXEC_RC__=96" in result.stdout
    assert "setup failed (exit 23)" in result.stderr
    assert not evaluator_ran.exists()


def test_evaluator_execs_in_container_and_returns_sentinel_rc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "checker.py").write_text("print('ok')\n")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
        ["python", "checker.py"],
        tmp_path,
        monkeypatch,
        exec_stdout="__VIBESYS_EXEC_RC__=3\n",
    )

    exec_argv = next(call for call in calls if call[3:5] == ["container", "exec"])
    assert result == 3
    assert exec_argv[:6] == [_UV_EXECUTABLE, "run", "modal", "container", "exec", "ta-123"]
    assert exec_argv[6:9] == ["--", "sh", "-c"]
    assert "python checker.py" in exec_argv[9]


def test_evaluator_stages_package_and_includes_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    (package / "adapter.py").write_text("print('ok')\n")

    (workspace / "main.py").write_text("app = object()\n")
    result, calls = _run_public_evaluator(
        workspace,
        ["python", ".vibesys-evaluator-package/adapter.py"],
        tmp_path,
        monkeypatch,
        setup_command=["sh", "-c", "echo prepared"],
        evaluator_package_root=package,
    )

    assert result == 0
    exec_argv = next(call for call in calls if call[3:5] == ["container", "exec"])
    assert "sh -c 'echo prepared'" in exec_argv[9]
    with _staged_archive(calls) as archive:
        assert ".vibesys-evaluator-package/adapter.py" in archive.getnames()


def test_evaluator_writes_relayed_output_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_path = tmp_path / "result.json"
    encoded = base64.b64encode(b'{"metric": 2}').decode()
    (workspace / "main.py").write_text("app = object()\n")
    result, _calls = _run_public_evaluator(
        workspace,
        ["python", "bench.py", "--output-json", str(output_path)],
        tmp_path,
        monkeypatch,
        exec_stdout=(
            f"__VIBESYS_OUTPUT_FILE__ {output_path}\n"
            f"{encoded}\n"
            "__VIBESYS_OUTPUT_END__\n"
            "__VIBESYS_EXEC_RC__=0\n"
        ),
    )

    assert result == 0
    assert json.loads(output_path.read_text()) == {"metric": 2}


def test_evaluator_reports_missing_rc_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "main.py").write_text("app = object()\n")
    result, _calls = _run_public_evaluator(
        workspace,
        ["python", "bench.py"],
        tmp_path,
        monkeypatch,
        exec_stdout="killed mid-run\n",
    )

    assert result == 1
    assert "did not report an exit code" in capsys.readouterr().err


def test_run_evaluator_deploys_waits_and_runs_colocated(monkeypatch: pytest.MonkeyPatch) -> None:
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
            [_UV_EXECUTABLE, "run", "modal", "deploy", "/workspace/main.py"],
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
        deployment=("https://workspace--candidate.modal.run", "candidate-app"),
        setup_command=None,
        evaluator_package_root=None,
    )


def test_run_evaluator_threads_trusted_setup_and_package(
    monkeypatch: pytest.MonkeyPatch,
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
        deployment=("https://workspace--candidate.modal.run", "candidate-app"),
        setup_command=("installer", "--locked"),
        evaluator_package_root=str(package.resolve()),
    )


def test_main_decodes_setup_command_and_forwards_package(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_run_evaluator_requires_app_identifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_run_evaluator_deploys_custom_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
            _UV_EXECUTABLE,
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        deployment=("https://workspace--candidate.modal.run", "candidate-app"),
        setup_command=None,
        evaluator_package_root=None,
    )


def test_run_evaluator_redeploys_when_lease_lacks_app_identifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    assert run.call_args_list[0].args[0][:4] == [_UV_EXECUTABLE, "run", "modal", "deploy"]
    colocated.assert_called_once()


def test_run_evaluator_releases_reused_deployment_after_final_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        [_UV_EXECUTABLE, "run", "modal", "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert not lease_path.exists()


def test_run_evaluator_releases_new_deployment_after_final_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        [_UV_EXECUTABLE, "run", "modal", "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert not lease_path.exists()


def test_run_evaluator_stops_mismatched_leased_app_before_redeploy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        [_UV_EXECUTABLE, "run", "modal", "app", "stop", "old-app", "--yes"],
        cwd="/workspace",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_run_evaluator_redeploys_and_replaces_mismatched_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
            [_UV_EXECUTABLE, "run", "modal", "app", "stop", "candidate-app", "--yes"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        in run.call_args_list
    )


@pytest.mark.parametrize(
    ("command", "error", "message"),
    [
        ("python", TypeError, "only argv strings"),
        (["python", 3], TypeError, "only argv strings"),
        ([], ValueError, "non-empty string argv"),
        ([""], ValueError, "executable must not be empty"),
    ],
)
def test_setup_command_rejects_malformed_argv(
    command: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        modal_evaluator.encode_setup_command(cast("Sequence[str]", command))


def test_setup_command_enforces_the_encoded_size_limit() -> None:
    limit = _MAX_ENCODED_SETUP_COMMAND_CHARS
    with pytest.raises(ValueError, match="exceeds the encoded size limit"):
        modal_evaluator.encode_setup_command(["x" * limit])
    with pytest.raises(ValueError, match="exceeds the encoded size limit"):
        _decode_setup_command("a" * (limit + 1))


def test_modal_health_url_only_accepts_modal_web_hosts() -> None:
    url = "https://workspace--candidate.modal.run"
    assert _modal_health_url(url) == f"{url}/health"
    for bad in ("http://workspace--candidate.modal.run", "https://evil.example", ""):
        with pytest.raises(ValueError, match="invalid web URL"):
            _modal_health_url(bad)


def _fake_response(status: int) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.__enter__.return_value = response
    return response


def test_wait_for_health_retries_until_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    urlopen = MagicMock(side_effect=[_fake_response(503), OSError("refused"), _fake_response(200)])
    sleeps: list[float] = []
    monkeypatch.setattr(modal_evaluator.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(modal_evaluator.time, "sleep", sleeps.append)

    modal_evaluator.wait_for_health("https://a--b.modal.run", timeout_seconds=1000)

    assert urlopen.call_count == 3
    assert urlopen.call_args.args[0] == "https://a--b.modal.run/health"
    assert sleeps == [2, 2]


def test_wait_for_health_times_out_with_the_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TimeoutError, match="did not become ready: no response"):
        modal_evaluator.wait_for_health("https://a--b.modal.run", timeout_seconds=0)

    ticks = iter([0.0, 0.0, 5.0])
    monkeypatch.setattr(modal_evaluator.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(modal_evaluator.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        modal_evaluator.urllib.request, "urlopen", MagicMock(return_value=_fake_response(502))
    )
    with pytest.raises(TimeoutError, match="did not become ready: HTTP 502"):
        modal_evaluator.wait_for_health("https://a--b.modal.run", timeout_seconds=1)


def test_wait_for_health_rejects_untrusted_urls_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urlopen = MagicMock()
    monkeypatch.setattr(modal_evaluator.urllib.request, "urlopen", urlopen)

    with pytest.raises(ValueError, match="invalid web URL"):
        modal_evaluator.wait_for_health("https://evil.example", timeout_seconds=1)

    urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (_fake_response(200), True),
        (_fake_response(503), False),
        (OSError("refused"), False),
    ],
)
def test_healthy_now_reflects_the_health_probe(
    monkeypatch: pytest.MonkeyPatch, outcome: object, expected: object
) -> None:
    if isinstance(outcome, Exception):
        urlopen = MagicMock(side_effect=outcome)
    else:
        urlopen = MagicMock(return_value=outcome)
    monkeypatch.setattr(modal_evaluator.urllib.request, "urlopen", urlopen)

    assert _healthy_now("https://a--b.modal.run") is expected
    assert _healthy_now("https://evil.example") is False


def test_recent_modal_logs_formats_bounded_output(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = _MAX_DIAGNOSTIC_CHARS
    results = iter(
        [
            SimpleNamespace(stdout="\x1b[31mred\x1b[0m line", stderr="err"),
            SimpleNamespace(stdout="", stderr=""),
            SimpleNamespace(stdout="a" * (limit + 10) + "TAIL", stderr=""),
        ]
    )
    run = MagicMock(side_effect=lambda *_a, **_k: next(results))
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)

    assert modal_evaluator.recent_modal_logs("app", workspace="/w") == "red line\nerr"
    assert (
        modal_evaluator.recent_modal_logs("app", workspace="/w") == "Modal returned no recent logs."
    )
    truncated = modal_evaluator.recent_modal_logs("app", workspace="/w")
    assert truncated.startswith("[... earlier Modal logs omitted ...]\n")
    assert truncated.endswith("TAIL")
    assert len(truncated) == len("[... earlier Modal logs omitted ...]\n") + limit
    argv = run.call_args.args[0]
    assert argv[argv.index("logs") + 1] == "app"
    assert run.call_args.kwargs["cwd"] == "/w"


def test_recent_modal_logs_reports_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modal_evaluator.subprocess, "run", MagicMock(side_effect=OSError("no uv")))

    assert (
        modal_evaluator.recent_modal_logs("app", workspace="/w")
        == "Could not fetch Modal logs: OSError: no uv"
    )


def test_stop_modal_app_reports_success_failure_and_launch_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=3, stdout="out", stderr="boom"),
            OSError("gone"),
        ]
    )

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)

    assert _stop_modal_app("app", workspace="/w") is True
    assert _stop_modal_app("app", workspace="/w") is False
    assert _stop_modal_app("app", workspace="/w") is False
    assert capsys.readouterr().err.splitlines() == [
        "Stopped Modal app app.",
        "Could not stop Modal app app (exit 3): out",
        "boom",
        "Could not stop Modal app app: OSError: gone",
    ]


def test_stage_archive_rejects_a_package_root_that_is_a_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = tmp_path / "package.txt"
    package.write_text("x")

    with pytest.raises(ValueError, match="evaluator package root is not a directory"):
        _build_stage_archive(str(workspace), [], evaluator_package_root=str(package))


def test_run_evaluator_validates_command_and_package_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing evaluator command after"):
        modal_evaluator.run_evaluator([], workspace=str(tmp_path))
    package = tmp_path / "package.txt"
    package.write_text("x")

    with pytest.raises(ValueError, match="evaluator package root is not a directory"):
        modal_evaluator.run_evaluator(
            ["true"], workspace=str(tmp_path), evaluator_package_root=str(package)
        )


def test_evaluator_relays_exec_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")

    def run(command: list[str], **_options: object) -> SimpleNamespace:
        if command[3:5] == ["container", "list"]:
            listing = [{"app_name": "candidate-app", "container_id": "ta-123"}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        return SimpleNamespace(returncode=0, stdout="__VIBESYS_EXEC_RC__=0\n", stderr="warn\n")

    keepwarm = MagicMock()
    keepwarm.return_value.__enter__ = MagicMock()
    keepwarm.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(modal_evaluator, "_DeploymentKeepWarm", keepwarm)
    monkeypatch.setattr(modal_evaluator.subprocess, "run", run)

    result = _execute_colocated(
        ["python", "main.py"],
        workspace=str(workspace),
        deployment=("https://workspace--candidate.modal.run", "candidate-app"),
    )

    assert result == 0
    assert capsys.readouterr().err == "warn\n"


def test_reused_deployment_setup_failure_is_reported_and_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setenv(_RELEASE_DEPLOYMENT_ENV, "1")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator, "_healthy_now", MagicMock(return_value=True))
    monkeypatch.setattr(
        modal_evaluator, "_execute_colocated", MagicMock(side_effect=ValueError("bad setup"))
    )
    stop = MagicMock(return_value=True)
    monkeypatch.setattr(modal_evaluator, "_stop_modal_app", stop)

    result = _execute_reused_candidate(
        ["true"], workspace="/workspace", setup_command=None, evaluator_package_root=None
    )

    assert result == 1
    err = capsys.readouterr().err
    assert "Reusing healthy Modal deployment for candidate revision abc123." in err
    assert "Modal evaluator setup failed: bad setup" in err
    stop.assert_called_once_with("candidate-app", workspace="/workspace")
    assert not lease_path.exists()
