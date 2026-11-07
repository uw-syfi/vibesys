from __future__ import annotations

import base64
import errno
import io
import json
import os
import shlex
import shutil
import stat
import sys
import tarfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, Unpack, cast
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_sandbox import (
    modal_evaluator,  # test-isolation: this suite exercises the installed helper's private process and lease state machines directly.
)
from vs_sandbox.modal_evaluator import (  # test-isolation: constants bound remote helper protocol limits that this suite verifies directly.
    _EVALUATOR_BOOTSTRAP_FAILURE_EXIT_CODE,
    _MAX_DIAGNOSTIC_CHARS,
    _MAX_ENCODED_SETUP_COMMAND_CHARS,
    _RELEASE_DEPLOYMENT_ENV,
    ModalHost,
    ProgramResult,
    _build_stage_archive,
    _decode_setup_command,
    _Deployment,
    _execute_colocated,
    _execute_reused_candidate,
    _healthy_now,
    _modal_health_url,
    _stop_modal_app,
)
from vs_sim.api.testing import HANG_GUARD_S, ManualClock

_UV_EXECUTABLE = modal_evaluator.shutil.which("uv") or "uv"
_PROJECT_MODAL = [_UV_EXECUTABLE, "run", "--quiet", "modal"]
"""The program that runs the candidate project's Modal CLI, before the CLI's arguments."""
_FRAMEWORK_MODAL = list(modal_evaluator.FRAMEWORK_MODAL_CLI)
"""The program that runs the framework's Modal client, before the CLI's arguments."""


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


def _modal_arguments(argv: list[str]) -> list[str]:
    """The Modal CLI arguments of one recorded run, after the program that runs the CLI."""
    return argv[argv.index("modal") + 1 :]


class AdvancingClock:
    """A manual clock whose blocking sleep moves it forward."""

    def __init__(self) -> None:
        """Start at zero."""
        self._clock = ManualClock(0.0)

    def now(self) -> float:
        """The current time."""
        return self._clock.now()

    def sleep(self, seconds: float) -> None:
        """Pause ``seconds`` manual seconds."""
        self._clock.advance(seconds)


@dataclass(frozen=True)
class ProgramCall:
    """One Modal CLI run the evaluator asked for."""

    argv: list[str]
    cwd: str
    timeout: float | None


class ScriptedPrograms:
    """Answers each program run from ``answer(argv)`` and records the call."""

    def __init__(self, answer: Callable[[list[str]], SimpleNamespace]) -> None:
        """Answer runs with ``answer``; it may raise ``OSError`` for a program that cannot start."""
        self._answer = answer
        self.calls: list[ProgramCall] = []

    def run(self, argv: Sequence[str], *, cwd: str, timeout: float | None) -> ProgramResult:
        """Record the run and return the scripted outcome."""
        self.calls.append(ProgramCall(list(argv), cwd, timeout))
        outcome = self._answer(list(argv))
        return ProgramResult(outcome.returncode, outcome.stdout, outcome.stderr)


def _refuse_every_program(argv: list[str]) -> SimpleNamespace:
    raise AssertionError(argv)


@dataclass
class ScriptedHealth:
    """Answers every health probe with ``status``, or raises ``error``; records each URL."""

    status: int = 200
    error: OSError | None = None
    urls: list[str] = field(default_factory=list)

    def __call__(self, url: str, _timeout: float) -> int:
        """Answer one probe."""
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.status


_DEPLOY_BEGAN = datetime(2026, 11, 7, 20, 59, tzinfo=UTC).timestamp()
"""The fake host's wall clock, in seconds since the epoch, when each deploy here starts."""
_DEPLOY_SECONDS = 43.0
"""How long ``_run_public_evaluator``'s ``modal deploy`` takes on that clock."""


def _modal_start_time(seconds_after_deploy_began: float) -> str:
    """A container's start as ``modal container list --json`` prints it: local offset, seconds."""
    started = datetime.fromtimestamp(
        _DEPLOY_BEGAN + seconds_after_deploy_began, timezone(timedelta(hours=-8))
    )
    return started.isoformat(sep=" ", timespec="seconds")


@dataclass
class _Host:
    """A fake evaluator host and the records a test reads back."""

    programs: ScriptedPrograms
    health: ScriptedHealth
    clock: AdvancingClock
    wall_clock: ManualClock
    warmed: list[str]

    @property
    def host(self) -> ModalHost:
        """The host the evaluator runs on."""
        return ModalHost(
            clock=self.clock,
            wall_clock=self.wall_clock,
            programs=self.programs,
            health=self.health,
            keep_warm=lambda base_url: self.warmed.append(base_url) or nullcontext(),
        )


def _fake_host(
    answer: Callable[[list[str]], SimpleNamespace] = _refuse_every_program,
    *,
    health: ScriptedHealth | None = None,
    clock: AdvancingClock | None = None,
    wall_clock: ManualClock | None = None,
) -> _Host:
    return _Host(
        programs=ScriptedPrograms(answer),
        health=ScriptedHealth() if health is None else health,
        clock=AdvancingClock() if clock is None else clock,
        wall_clock=ManualClock(_DEPLOY_BEGAN) if wall_clock is None else wall_clock,
        warmed=[],
    )


class _EvaluatorOptions(TypedDict, total=False):
    evaluator_package_root: Path | None
    setup_command: list[str] | None
    deploy_stdout: str
    container_listing: SimpleNamespace | None
    exec_stdout: str
    health: ScriptedHealth
    clock: AdvancingClock
    container_discovery_timeout_seconds: float


_DEPLOY_STDOUT = (
    "Web Function URL: https://workspace--candidate.modal.run\n"
    "View Deployment: https://modal.com/apps/workspace/main/deployed/candidate-app\n"
)


_FRESH_DEPLOYMENT = _Deployment(
    "https://workspace--candidate.modal.run",
    "candidate-app",
    started_since=modal_evaluator.started_since_deploy(_DEPLOY_BEGAN, _DEPLOY_STDOUT),
)
"""What a deploy at ``_DEPLOY_BEGAN`` that printed ``_DEPLOY_STDOUT`` hands the colocated run."""
_REUSED_DEPLOYMENT = _Deployment(
    "https://workspace--candidate.modal.run", "candidate-app", started_since=None
)
"""A leased deployment: no deploy ran, so any container of the app runs it."""


def _listing(*entries: dict[str, str]) -> SimpleNamespace:
    """A successful ``modal container list --json`` of ``entries``."""
    return SimpleNamespace(returncode=0, stdout=json.dumps(list(entries)), stderr="")


@pytest.fixture(autouse=True)
def isolate_deployment_path_checks(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep Modal CLI tests focused on orchestration, not the fake /workspace."""
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

    def run_deploy(command: list[str]) -> SimpleNamespace:
        deployed_paths.append(str(command[-1]))
        return SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")

    monkeypatch.setattr(modal_evaluator, "_execute_colocated", lambda *_args, **_kwargs: 0)
    modal_evaluator.run_evaluator(
        ["true"], workspace=str(workspace), entrypoint=entrypoint, host=_fake_host(run_deploy).host
    )
    return deployed_paths


def _run_with_lock_path(lock_path: Path, tmp_path: Path) -> int:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", lock_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    try:
        return modal_evaluator.run_evaluator(
            ["true"], workspace=str(workspace), host=_fake_host().host
        )
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
    wall_clock = ManualClock(_DEPLOY_BEGAN)

    def run(command: list[str]) -> SimpleNamespace:
        calls.append(command)
        if _modal_arguments(command)[0] == "deploy":
            wall_clock.advance(_DEPLOY_SECONDS)
            stdout = options.get("deploy_stdout", _DEPLOY_STDOUT)
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if _modal_arguments(command)[:2] == ["container", "list"]:
            return container_listing or _listing(
                {
                    "app_name": "candidate-app",
                    "container_id": "ta-123",
                    "start_time": _modal_start_time(_DEPLOY_SECONDS + 5),
                }
            )
        return SimpleNamespace(
            returncode=0,
            stdout=exec_stdout,
            stderr="",
        )

    monkeypatch.setattr(modal_evaluator, "_LOCK_PATH", tmp_path / "runtime" / "lock")
    result = modal_evaluator.run_evaluator(
        command,
        workspace=str(workspace),
        evaluator_package_root=(
            str(evaluator_package_root) if evaluator_package_root is not None else None
        ),
        setup_command=setup_command,
        container_discovery_timeout_seconds=options.get(
            "container_discovery_timeout_seconds", 90.0
        ),
        host=_fake_host(
            run, health=options.get("health"), clock=options.get("clock"), wall_clock=wall_clock
        ).host,
    )
    return result, calls


def _staged_archive(calls: list[list[str]]) -> tarfile.TarFile:
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    payload = base64.b64decode("".join(_modal_arguments(exec_command)[8:]))
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


def test_runtime_dir_prefers_xdg_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public evaluation creates its lock under XDG_RUNTIME_DIR when configured."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    imported = _load_modal_evaluator(monkeypatch, "xdg")

    assert imported.run_evaluator(["true"], workspace=str(workspace), host=_fake_host().host) == 1
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
        modal_evaluator.extract_modal_web_url(ProgramResult(0, output, ""))
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
        modal_evaluator.extract_modal_web_url(ProgramResult(0, output, ""))
        == "https://vibeserve--vibesys-long-candidate-f51b76.modal.run"
    )


def test_extract_modal_web_url_requires_endpoint() -> None:
    with pytest.raises(ValueError, match="did not print"):
        modal_evaluator.extract_modal_web_url(
            ProgramResult(0, "App deployed without a web function", "")
        )


def test_extract_modal_app_identifier_handles_rich_line_wrapping() -> None:
    output = """
    View Deployment:
    │ https://modal.com/apps/workspace/main/deployed/vibesys-long-
    │ candidate
    """

    assert (
        modal_evaluator.extract_modal_app_identifier(ProgramResult(0, output, ""))
        == "vibesys-long-candidate"
    )


def test_setup_command_encoding_round_trips_opaque_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = ("sh", "-c", "printf '%s' '$HOME; touch nope'", "")

    encoded = modal_evaluator.encode_setup_command(command)
    parsed: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        modal_evaluator,
        "run_evaluator",
        lambda *args, **kwargs: parsed.append((args, kwargs)) or 0,
    )
    assert modal_evaluator.main(["--setup-command-base64", encoded, "--", "true"]) == 0

    [(argv, options)] = parsed
    host = options.pop("host")
    assert isinstance(host, ModalHost)
    assert isinstance(host.clock, modal_evaluator.MonotonicWaits)
    assert isinstance(host.wall_clock, modal_evaluator.SystemWallClock)
    assert [(argv, options)] == [
        (
            (["true"],),
            {
                "workspace": "/workspace",
                "entrypoint": "main.py",
                "readiness_timeout_seconds": 90,
                "container_discovery_timeout_seconds": 90.0,
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
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    assert str(outputs_parent / "result.json") in _modal_arguments(exec_command)[6]


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
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)


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
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)


def test_evaluator_selects_matching_snake_case_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    started = _modal_start_time(_DEPLOY_SECONDS + 5)
    listing = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            [
                {"container_id": "ta-other", "app_name": "other-app", "start_time": started},
                {"container_id": "ta-123", "app_name": "candidate-app", "start_time": started},
            ]
        ),
        stderr="",
    )

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    assert result == 0
    assert _modal_arguments(exec_command)[2] == "ta-123"


def test_evaluator_selects_matching_title_case_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    started = _modal_start_time(_DEPLOY_SECONDS + 5)
    listing = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            [{"Container ID": "ta-123", "App Name": "candidate-app", "Start Time": started}]
        ),
        stderr="",
    )

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    assert result == 0
    assert _modal_arguments(exec_command)[2] == "ta-123"


def _container(container_id: str, start_time: str) -> dict[str, str]:
    """A container of the candidate's app as Modal 1.4 lists it (newer clients use snake_case)."""
    return {
        "Container ID": container_id,
        "App ID": "ap-candidate",
        "App Name": "candidate-app",
        "Start Time": start_time,
    }


@pytest.mark.regression
def test_after_a_deploy_the_gate_runs_in_the_new_versions_container_not_an_older_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #4097: the first container listed can predate the deploy and run old code.

    The new version's container started while ``modal deploy`` still ran, so only a floor read
    before the deploy accepts it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = _listing(
        _container("ta-old", _modal_start_time(-301)),
        _container("ta-new", _modal_start_time(10)),
    )

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    assert result == 0
    assert _modal_arguments(exec_command)[2] == "ta-new"


@pytest.mark.regression
def test_after_a_deploy_an_older_or_unstarted_container_is_never_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for #4097: with only those listed, the gate fails and names them."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = _listing(
        _container("ta-old", _modal_start_time(-301)), _container("ta-pending", "Pending")
    )

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )

    assert result == 1
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)
    err = capsys.readouterr().err
    assert "no running container found for Modal app candidate-app" in err
    assert "no listed container started since this deploy began" in err
    assert "ta-old" in err
    assert "ta-pending" in err


def test_a_deploy_that_changed_nothing_keeps_measuring_the_running_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    skipped = f"✓ Deployment skipped: no changes detected! ⏭️\n{_DEPLOY_STDOUT}"

    result, calls = _run_public_evaluator(
        workspace,
        ["python", "main.py"],
        tmp_path,
        monkeypatch,
        deploy_stdout=skipped,
        container_listing=_listing(_container("ta-running", _modal_start_time(-301))),
    )

    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    assert result == 0
    assert _modal_arguments(exec_command)[2] == "ta-running"


def test_evaluator_reports_container_list_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = SimpleNamespace(returncode=2, stdout="", stderr="token expired")

    result, calls = _run_public_evaluator(
        workspace, ["python", "main.py"], tmp_path, monkeypatch, container_listing=listing
    )
    assert result == 1
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)
    err = capsys.readouterr().err
    assert "container list exited 2: token expired" in err
    assert "capacity" not in err, "a listing that failed says nothing about capacity"


def test_evaluator_reports_missing_running_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    listing = SimpleNamespace(returncode=0, stdout="[]", stderr="")
    health = ScriptedHealth()
    result, calls = _run_public_evaluator(
        workspace,
        ["python", "main.py"],
        tmp_path,
        monkeypatch,
        container_listing=listing,
        health=health,
    )
    assert result == 1
    assert len(health.urls) >= 2
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)
    assert "no running container found" in capsys.readouterr().err


@pytest.mark.regression
@pytest.mark.parametrize("bound", [1.0, 90.0, 7200.0])
def test_container_discovery_waits_as_long_as_its_caller_bounds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bound: float,
) -> None:
    """Regression (#4099): discovery gave a container a fixed 90 s, whatever the gate asked for.

    A scarce GPU's container can queue for capacity after readiness too, so the wait for it
    to be listed follows the bound the run environment passes, and its failure says that
    Modal may still be waiting for capacity.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    clock = AdvancingClock()

    result, calls = _run_public_evaluator(
        workspace,
        ["python", "main.py"],
        tmp_path,
        monkeypatch,
        container_listing=SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        container_discovery_timeout_seconds=bound,
        clock=clock,
    )

    assert result == 1
    assert clock.now() == bound
    assert not any(_modal_arguments(call)[:2] == ["container", "exec"] for call in calls)
    err = capsys.readouterr().err
    assert (
        f"no running container found for Modal app candidate-app: none started within {bound:g} s; "
        "Modal may still be waiting for capacity for its GPU"
    ) in err


@pytest.mark.parametrize(
    ("listing", "said", "capacity"),
    [
        pytest.param(
            _listing(),
            "No container of Modal app candidate-app started running the deployed version "
            "within 30 s: Modal may still be waiting for capacity for its GPU.",
            True,
            id="none-listed",
        ),
        pytest.param(
            _listing(_container("ta-pending", "Pending")),
            "No container of Modal app candidate-app started running the deployed version "
            "within 30 s (passed over: ta-pending (start time 'Pending')): "
            "Modal may still be waiting for capacity for its GPU.",
            True,
            id="pending",
        ),
        pytest.param(
            _listing(_container("ta-1", _modal_start_time(5))),
            "Container ta-1 of Modal app candidate-app is running but did not answer /health "
            "within 30 s.",
            False,
            id="running",
        ),
        pytest.param(
            SimpleNamespace(returncode=2, stdout="", stderr="token expired"),
            "Whether a container of Modal app candidate-app is running is unknown: "
            "container list exited 2: token expired.",
            False,
            id="unlisted",
        ),
    ],
)
def test_a_readiness_timeout_says_whether_the_app_had_a_running_container(
    capsys: pytest.CaptureFixture[str], listing: SimpleNamespace, said: str, *, capacity: bool
) -> None:
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")

    def run(argv: list[str]) -> SimpleNamespace:
        return listing if _modal_arguments(argv)[:2] == ["container", "list"] else deploy

    fake = _fake_host(run, health=ScriptedHealth(error=OSError("timed out")))

    result = modal_evaluator.run_evaluator(
        ["true"], workspace="/workspace", readiness_timeout_seconds=30, host=fake.host
    )

    assert result == 1
    err = capsys.readouterr().err
    assert said in err
    assert ("capacity" in err) == capacity
    assert (
        ProgramCall([*_FRAMEWORK_MODAL, "container", "list", "--json"], "/workspace", 60)
        in fake.programs.calls
    ), "the container state after a readiness timeout is read with the framework's client"
    assert (
        ProgramCall(
            [*_FRAMEWORK_MODAL, "app", "stop", "candidate-app", "--yes"],
            cwd="/workspace",
            timeout=60,
        )
        in fake.programs.calls
    )


@pytest.mark.parametrize(
    "flag", ["--readiness-timeout-seconds", "--container-discovery-timeout-seconds"]
)
@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "soon"])
def test_a_container_wait_must_be_a_positive_finite_number_of_seconds(
    flag: str, value: str
) -> None:
    with pytest.raises(SystemExit):
        modal_evaluator.main([flag, value, "--", "true"])


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
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    script = _modal_arguments(exec_command)[6]

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
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    script = _modal_arguments(exec_command)[6]

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
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    script = _modal_arguments(exec_command)[6]
    encoded = "".join(_modal_arguments(exec_command)[8:])
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


_BOOTSTRAP_TOOLS = ("base64", "chmod", "dirname", "gzip", "ln", "mkdir", "rm", "sh", "tar")
_PIP_INSTALLS_UV = """\
target=
previous=
for argument; do
  [ "$previous" = --target ] && target=$argument
  previous=$argument
done
mkdir -p "$target/uv"
exit 0
"""
_PIP_IS_MISSING = """\
echo "$0: No module named pip" >&2
exit 1
"""
# The bootstrap's shim runs ``python3 -I -c <runpy of uv> <pip root> <uv arguments>``. The
# real interpreter's part of it (loading ``uv`` from the pip root alone) is
# ``test_evaluator_bootstrap_runs_command_verbatim_and_relays_outputs``'s.
_PYTHON3 = """\
#!/bin/sh
case "$1 $2" in
  "-I -m")
{pip};;
  "-I -c")
    [ -d "$4/uv" ] || exit 1
    echo 'uv from pip'
    exit 0;;
esac
echo "unexpected python3 call: $*" >&2
exit 2
"""


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def _bootstrap_path(tmp_path: Path, *, pip_works: bool, image_uv: Path | None) -> str:
    """A ``PATH`` holding only the bootstrap's tools, a ``python3``, and ``image_uv``'s directory.

    ``python3 -m pip`` either installs a ``uv`` module, which the shim then runs, printing
    ``uv from pip``, or fails as in a virtual environment uv made. ``mktemp`` makes the stage
    under ``tmp_path``. No other ``uv`` is reachable.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in _BOOTSTRAP_TOOLS:
        found = shutil.which(tool)
        assert found is not None, tool
        (tools / tool).symlink_to(found)
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    stages = tmp_path / "stages"
    stages.mkdir()
    _write_executable(
        tools / "mktemp",
        f"#!/bin/sh\nexec {shlex.quote(real_mktemp)} -d {shlex.quote(str(stages))}/eval-XXXXXX\n",
    )
    pip = _PIP_INSTALLS_UV if pip_works else _PIP_IS_MISSING
    _write_executable(tools / "python3", _PYTHON3.format(pip=pip))
    if image_uv is None:
        return str(tools)
    return f"{tools}{os.pathsep}{image_uv.parent}"


@pytest.mark.regression
@pytest.mark.parametrize(
    ("pip_works", "image_has_uv", "uv_from"),
    [
        pytest.param(True, True, "uv from pip", id="pip-and-image-uv"),
        pytest.param(True, False, "uv from pip", id="pip-only"),
        pytest.param(False, True, "uv from the image", id="image-uv-only"),
        pytest.param(False, False, None, id="neither"),
    ],
)
def test_the_bootstrap_installs_uv_with_pip_else_runs_the_images_uv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pip_works: bool,
    image_has_uv: bool,
    uv_from: str | None,
) -> None:
    """Regression for #4096: images built by Modal's ``uv_sync`` have a ``uv`` but no pip.

    Pip stays first, so an image's own ``uv`` answers only when pip cannot install one.
    """
    if uv_from is None and os.access("/.uv/uv", os.X_OK):
        pytest.skip("this host has Modal's image uv at /.uv/uv, so the bootstrap finds a uv")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")
    image_uv = tmp_path / "image" / "uv"
    image_uv.parent.mkdir()
    _write_executable(image_uv, "#!/bin/sh\necho 'uv from the image'\n")
    _result, calls = _run_public_evaluator(workspace, ["uv", "--version"], tmp_path, monkeypatch)
    exec_command = next(
        call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"]
    )
    path = _bootstrap_path(
        tmp_path, pip_works=pip_works, image_uv=image_uv if image_has_uv else None
    )

    result = run_test_command(
        [
            "sh",
            "-c",
            _modal_arguments(exec_command)[6],
            "vibesys-eval",
            "".join(_modal_arguments(exec_command)[8:]),
        ],
        capture_output=True,
        check=False,
        env={"PATH": path},
        text=True,
        timeout=HANG_GUARD_S,
    )

    assert result.returncode == 0, result.stderr
    if uv_from is None:
        failure = f"__VIBESYS_EXEC_RC__={_EVALUATOR_BOOTSTRAP_FAILURE_EXIT_CODE}"
        assert result.stdout.split() == [failure]
        assert "neither python3 -m pip nor uv" in result.stderr
    else:
        assert result.stdout.split("\n") == [uv_from, "", "__VIBESYS_EXEC_RC__=0", ""]
        ran_image_uv = uv_from == "uv from the image"
        assert (str(image_uv) in result.stderr) == ran_image_uv, "it names the image uv it runs"


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

    exec_argv = next(call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"])
    assert result == 3
    assert exec_argv[: len(_FRAMEWORK_MODAL)] == _FRAMEWORK_MODAL
    assert _modal_arguments(exec_argv)[:6] == ["container", "exec", "ta-123", "--", "sh", "-c"]
    assert "python checker.py" in _modal_arguments(exec_argv)[6]


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
    exec_argv = next(call for call in calls if _modal_arguments(call)[:2] == ["container", "exec"])
    assert "sh -c 'echo prepared'" in _modal_arguments(exec_argv)[6]
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
    fake = _fake_host(MagicMock(return_value=deploy))
    host = fake.host
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=host
    )

    assert result == 0
    assert fake.programs.calls == [
        ProgramCall(
            [*_PROJECT_MODAL, "deploy", "/workspace/main.py"],
            cwd="/workspace",
            timeout=None,
        ),
    ]
    assert fake.health.urls == ["https://workspace--candidate.modal.run/health"]
    colocated.assert_called_once_with(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
        deployment=_FRESH_DEPLOYMENT,
        host=host,
        setup_command=None,
        evaluator_package_root=None,
        container_discovery_timeout_seconds=90.0,
    )


def test_an_unready_deployment_is_given_up_on_at_the_readiness_deadline_of_the_given_clock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    colocated: list[object] = []
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", lambda *a, **_k: colocated.append(a))
    fake = _fake_host(lambda _argv: deploy, health=ScriptedHealth(error=OSError()))

    result = modal_evaluator.run_evaluator(
        ["true"], workspace="/workspace", readiness_timeout_seconds=30, host=fake.host
    )

    assert result == 1
    assert not colocated
    assert fake.clock.now() == 30
    assert "did not become ready" in capsys.readouterr().err


@given(timeout=st.integers(min_value=1, max_value=300))
def test_a_missing_container_is_given_up_on_at_the_discovery_deadline_of_the_given_clock(
    timeout: int,
) -> None:
    clock = AdvancingClock()
    probes: list[float] = []

    def warm(_base_url: str) -> bool:
        probes.append(clock.now())
        return True

    with pytest.raises(TimeoutError):
        modal_evaluator.find_app_container(
            "candidate-app",
            started_since=None,
            workspace="/workspace",
            base_url="https://workspace--candidate.modal.run",
            timeout_seconds=timeout,
            clock=clock,
            list_containers=lambda _workspace: modal_evaluator.ContainerListing(()),
            warm=warm,
        )

    assert clock.now() == timeout
    assert probes
    assert max(probes) < timeout


def test_run_evaluator_threads_trusted_setup_and_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    host = _fake_host(MagicMock(return_value=deploy)).host
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace="/workspace",
        setup_command=["installer", "--locked"],
        evaluator_package_root=str(package),
        host=host,
    )

    assert result == 0
    colocated.assert_called_once_with(
        ["python", ".vibesys-evaluator-package/adapter.py"],
        workspace="/workspace",
        deployment=_FRESH_DEPLOYMENT,
        host=host,
        setup_command=("installer", "--locked"),
        evaluator_package_root=str(package.resolve()),
        container_discovery_timeout_seconds=90.0,
    )


def test_main_decodes_setup_command_and_forwards_package(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(return_value=4)
    monkeypatch.setattr(modal_evaluator, "run_evaluator", run)
    encoded = modal_evaluator.encode_setup_command(["installer", "argument with spaces"])

    result = modal_evaluator.main(
        [
            "--workspace",
            "/workspace",
            "--readiness-timeout-seconds",
            "7200",
            "--container-discovery-timeout-seconds",
            "7200",
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
    run.assert_called_once()
    args, options = run.call_args
    options = dict(options)
    host = options.pop("host")
    assert isinstance(host, ModalHost)
    assert isinstance(host.clock, modal_evaluator.MonotonicWaits)
    assert isinstance(host.wall_clock, modal_evaluator.SystemWallClock)
    assert (args, options) == (
        (["python", ".vibesys-evaluator-package/adapter.py"],),
        {
            "workspace": "/workspace",
            "entrypoint": "main.py",
            "readiness_timeout_seconds": 7200.0,
            "container_discovery_timeout_seconds": 7200.0,
            "setup_command": ("installer", "argument with spaces"),
            "evaluator_package_root": "/opt/vibesys-evaluator-package",
        },
    )


def test_run_evaluator_requires_app_identifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy = SimpleNamespace(
        returncode=0,
        stdout="Web Function URL: https://workspace--candidate.modal.run\n",
        stderr="",
    )
    colocated = MagicMock(return_value=0)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
        host=_fake_host(MagicMock(return_value=deploy)).host,
    )

    assert result == 1
    colocated.assert_not_called()
    assert "did not print a deployment URL" in capsys.readouterr().err


def test_run_evaluator_deploys_custom_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    fake = _fake_host(MagicMock(return_value=deploy))
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["checker"],
        workspace="/workspace",
        entrypoint="examples/deployment/service.py",
        host=fake.host,
    )

    assert result == 0
    assert fake.programs.calls[0] == ProgramCall(
        [
            *_PROJECT_MODAL,
            "deploy",
            "/workspace/examples/deployment/service.py",
        ],
        cwd="/workspace",
        timeout=None,
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
    colocated = MagicMock(return_value=0)
    monkeypatch.setenv("VIBESYS_CANDIDATE_REVISION", "abc123")
    monkeypatch.setattr(modal_evaluator, "_DEPLOYMENT_LEASE_PATH", lease_path)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)
    fake = _fake_host()
    host = fake.host

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=host
    )

    assert result == 0
    assert fake.health.urls == ["https://workspace--candidate.modal.run/health"]
    assert fake.programs.calls == []
    colocated.assert_called_once_with(
        ["uv", "run", "python", "checker.py"],
        workspace="/workspace",
        deployment=_REUSED_DEPLOYMENT,
        host=host,
        setup_command=None,
        evaluator_package_root=None,
        container_discovery_timeout_seconds=90.0,
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
    fake = _fake_host(run)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", colocated)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 0
    assert run.call_args_list[0].args[0][:5] == [*_PROJECT_MODAL, "deploy"]
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
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))
    fake = _fake_host(run)

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 0
    assert fake.programs.calls[-1] == ProgramCall(
        [*_FRAMEWORK_MODAL, "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
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
    fake = _fake_host(run)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 0
    assert fake.programs.calls[-1] == ProgramCall(
        [*_FRAMEWORK_MODAL, "app", "stop", "candidate-app", "--yes"],
        cwd="/workspace",
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
    fake = _fake_host(run)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 0
    assert fake.programs.calls[0] == ProgramCall(
        [*_FRAMEWORK_MODAL, "app", "stop", "old-app", "--yes"],
        cwd="/workspace",
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
    fake = _fake_host(run)
    monkeypatch.setattr(modal_evaluator, "_execute_colocated", MagicMock(return_value=0))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 0
    assert json.loads(lease_path.read_text()) == {
        "candidate_revision": "new",
        "base_url": "https://workspace--candidate.modal.run",
        "app_identifier": "candidate-app",
    }


def test_run_evaluator_prints_modal_logs_when_readiness_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    deploy = SimpleNamespace(returncode=0, stdout=_DEPLOY_STDOUT, stderr="")
    logs = SimpleNamespace(returncode=0, stdout="RuntimeError: CUDA toolkit mismatch", stderr="")

    def run(command: list[str]) -> SimpleNamespace:
        return logs if _modal_arguments(command)[:2] == ["app", "logs"] else deploy

    fake = _fake_host(run, health=ScriptedHealth(status=503))

    result = modal_evaluator.run_evaluator(
        ["uv", "run", "python", "checker.py"], workspace="/workspace", host=fake.host
    )

    assert result == 1
    logs_fetched = [
        _modal_arguments(call.argv)[2]
        for call in fake.programs.calls
        if _modal_arguments(call.argv)[:2] == ["app", "logs"]
    ]
    assert logs_fetched == ["candidate-app"]
    assert "RuntimeError: CUDA toolkit mismatch" in capsys.readouterr().err
    assert (
        ProgramCall(
            [*_FRAMEWORK_MODAL, "app", "stop", "candidate-app", "--yes"],
            cwd="/workspace",
            timeout=60,
        )
        in fake.programs.calls
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


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [(200, None, True), (503, None, False), (200, OSError("refused"), False)],
)
def test_healthy_now_reflects_the_health_probe(
    status: int, error: OSError | None, expected: object
) -> None:
    health = ScriptedHealth(status=status, error=error)

    assert _healthy_now("https://a--b.modal.run", health) is expected
    assert _healthy_now("https://evil.example", health) is False
    assert health.urls == ["https://a--b.modal.run/health"]


def test_recent_modal_logs_formats_bounded_output() -> None:
    limit = _MAX_DIAGNOSTIC_CHARS
    results = iter(
        [
            SimpleNamespace(stdout="\x1b[31mred\x1b[0m line", stderr="err"),
            SimpleNamespace(stdout="", stderr=""),
            SimpleNamespace(stdout="a" * (limit + 10) + "TAIL", stderr=""),
        ]
    )
    programs = ScriptedPrograms(lambda _argv: SimpleNamespace(returncode=0, **vars(next(results))))

    def logs() -> str:
        return modal_evaluator.recent_modal_logs("app", workspace="/w", programs=programs)

    assert logs() == "red line\nerr"
    assert logs() == "Modal returned no recent logs."
    truncated = logs()
    assert truncated.startswith("[... earlier Modal logs omitted ...]\n")
    assert truncated.endswith("TAIL")
    assert len(truncated) == len("[... earlier Modal logs omitted ...]\n") + limit
    argv = programs.calls[-1].argv
    assert argv[argv.index("logs") + 1] == "app"
    assert programs.calls[-1].cwd == "/w"


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (OSError("no uv"), "OSError: no uv"),
        (
            TimeoutError("uv timed out after 30 seconds"),
            "TimeoutError: uv timed out after 30 seconds",
        ),
    ],
)
def test_recent_modal_logs_reports_fetch_failure(error: OSError, shown: str) -> None:
    programs = ScriptedPrograms(MagicMock(side_effect=error))

    assert (
        modal_evaluator.recent_modal_logs("app", workspace="/w", programs=programs)
        == f"Could not fetch Modal logs: {shown}"
    )


def test_stop_modal_app_reports_success_failure_and_launch_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcomes = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=3, stdout="out", stderr="boom"),
            OSError("gone"),
        ]
    )

    def run(_argv: list[str]) -> SimpleNamespace:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    programs = ScriptedPrograms(run)

    assert _stop_modal_app("app", workspace="/w", programs=programs) is True
    assert _stop_modal_app("app", workspace="/w", programs=programs) is False
    assert _stop_modal_app("app", workspace="/w", programs=programs) is False
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
        modal_evaluator.run_evaluator([], workspace=str(tmp_path), host=_fake_host().host)
    package = tmp_path / "package.txt"
    package.write_text("x")

    with pytest.raises(ValueError, match="evaluator package root is not a directory"):
        modal_evaluator.run_evaluator(
            ["true"],
            workspace=str(tmp_path),
            evaluator_package_root=str(package),
            host=_fake_host().host,
        )


def test_evaluator_relays_exec_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("app = object()\n")

    def run(command: list[str]) -> SimpleNamespace:
        if _modal_arguments(command)[:2] == ["container", "list"]:
            listing = [{"app_name": "candidate-app", "container_id": "ta-123"}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        return SimpleNamespace(returncode=0, stdout="__VIBESYS_EXEC_RC__=0\n", stderr="warn\n")

    fake = _fake_host(run)

    result = _execute_colocated(
        ["python", "main.py"],
        workspace=str(workspace),
        deployment=_REUSED_DEPLOYMENT,
        host=fake.host,
    )

    assert result == 0
    assert fake.warmed == ["https://workspace--candidate.modal.run"]
    assert fake.programs.calls[-1].timeout is None
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
    fake = _fake_host(lambda _argv: SimpleNamespace(returncode=0, stdout="", stderr=""))

    def execute(deployment: modal_evaluator._Deployment) -> int:
        assert (deployment.base_url, deployment.app_identifier, deployment.started_since) == (
            "https://workspace--candidate.modal.run",
            "candidate-app",
            None,
        )
        message = "bad setup"
        raise ValueError(message)

    result = _execute_reused_candidate(execute, workspace="/workspace", host=fake.host)

    assert result == 1
    err = capsys.readouterr().err
    assert "Reusing healthy Modal deployment for candidate revision abc123." in err
    assert "Modal evaluator setup failed: bad setup" in err
    assert fake.programs.calls == [
        ProgramCall(
            [*_FRAMEWORK_MODAL, "app", "stop", "candidate-app", "--yes"],
            cwd="/workspace",
            timeout=60,
        )
    ]
    assert not lease_path.exists()
