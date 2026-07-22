from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibesys.agents import cli_docker
from vibesys.agents.cli_docker import DockerAuthPath
from vibesys.backends import SandboxKind
from vibesys.domains.environment import EnvironmentBindMount
from vibesys.sandbox.run_environment import (
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
)


class FakeBackend:
    image = "fake-image"

    def __init__(self) -> None:
        self.sandbox = MagicMock()
        self.calls = []

    def make_sandbox(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        return self.sandbox


def _request(tmp_path: Path, backend: FakeBackend, **overrides):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    values = dict(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="deepagents",
        cli_provider=None,
    )
    values.update(overrides)
    values["log_dir"].mkdir(exist_ok=True)
    return RunEnvironmentRequest(**values)


def test_local_environment_opens_local_sandbox_with_host_paths(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("local"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
        )
    )

    assert backend.calls[0][0] is SandboxKind.LOCAL
    assert session.sandbox is backend.sandbox
    assert session.view.paths.accuracy_command == "uv run python accuracy_checker/checker.py"
    assert session.view.paths.benchmark_command == "uv run python benchmark/benchmark.py"
    assert session.view.isolated is False
    backend.sandbox.start.assert_not_called()


def test_docker_environment_opens_one_started_sandbox_with_agent_paths(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
        )
    )

    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert session.view.isolated is True
    assert session.view.cli_sandboxed is True
    assert session.view.paths.accuracy_command == "uv run python accuracy_checker/checker.py"
    assert session.view.paths.benchmark_command == "uv run python benchmark/benchmark.py"
    backend.sandbox.start.assert_called_once()

    session.close()
    backend.sandbox.stop.assert_called_once()


def test_docker_environment_copies_cli_auth_from_readonly_staging(tmp_path, monkeypatch):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    auth_file = tmp_path / "synthetic-codex-home" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text('{"synthetic": true}\n')
    monkeypatch.setitem(
        cli_docker.DOCKER_AUTH_PATHS,
        "codex",
        [DockerAuthPath(auth_file, "/root/.codex/auth.json")],
    )

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    kwargs = backend.calls[0][1]
    assert (str(auth_file), "/opt/vibesys-auth/0", True) in kwargs["bind_mounts"]
    assert kwargs["extra_init_commands"][0] == (
        "mkdir -p /root/.codex && cp -a /opt/vibesys-auth/0 /root/.codex/auth.json"
    )


def test_docker_environment_uses_environment_bind_mounts(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    env.open(
        _request(
            tmp_path,
            backend,
            environment_bind_mounts=(EnvironmentBindMount(model_dir, "/model", True),),
        )
    )

    kwargs = backend.calls[0][1]
    assert (str(model_dir), "/model", True) in kwargs["bind_mounts"]
    assert "/model" in kwargs["passthrough_paths"]


def test_docker_environment_mounts_selected_profiler_support(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    support = tmp_path / "custom-profiler"
    support.mkdir()

    session = env.open(
        _request(
            tmp_path,
            backend,
            profiler_support_path=str(support),
            profiler_support_name="fixture_profiler",
        )
    )

    kwargs = backend.calls[0][1]
    assert (str(support), "/workspace/fixture_profiler", True) in kwargs["bind_mounts"]
    assert session.view.paths.profiler_support == "fixture_profiler"


def test_docker_environment_does_not_infer_model_mount_from_reference_dir(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    ref_dir = tmp_path / "reference"
    (ref_dir / "model").mkdir(parents=True)

    env.open(_request(tmp_path, backend, ref_dir=ref_dir))

    bind_mounts = backend.calls[0][1]["bind_mounts"]
    assert all(container_path != "/model" for _, container_path, _ in bind_mounts)


def test_environment_session_context_manager_closes(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))

    with env.open(_request(tmp_path, backend)) as session:
        assert session.sandbox is backend.sandbox
        backend.sandbox.stop.assert_not_called()

    backend.sandbox.stop.assert_called_once()
    session.close()
    backend.sandbox.stop.assert_called_once()


def test_modal_environment_uses_local_docker_for_editing(tmp_path):
    """Post-refactor (April 2026): Modal mode runs the agent in a local
    Docker container; only GPU-bound work the implementer dispatches via
    `modal run` actually touches Modal."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    # The sandbox is local Docker, not a Modal Sandbox.
    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert session.view.cli_sandboxed is True
    # codex is NOT inside a Modal sandbox anymore — flag must be False.
    assert session.view.cli_modal_sandboxed is False
    backend.sandbox.start.assert_called_once()


def test_modal_environment_installs_modal_sdk_in_docker(tmp_path):
    """The local Docker container needs the Modal Python SDK installed so
    the implementer-authored `modal run` calls work."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    commands = backend.calls[0][1]["extra_init_commands"]
    assert any("pip install" in c and "modal" in c for c in commands), (
        f"expected `pip install modal` in init commands, got: {commands}"
    )


def test_modal_environment_prompt_notes_describe_modal_dispatch(tmp_path):
    """The view's prompt_notes should explain to agents that GPU work
    dispatches via `modal run main.py::<function>` and tell them how to
    discover pre-staged model volumes — without hardcoding any specific
    model IDs / volume names / mount paths."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))

    notes = session.view.prompt_notes
    assert "modal run" in notes
    assert "@app.cls" in notes or "@app.function" in notes
    assert "GPU" in notes
    # Tell the agent where to look up volume names rather than baking them in.
    assert "meta.json" in notes
    # No hardcoded model IDs or vibesys-internal volume names should leak
    # into the runtime-notes block.
    forbidden = (
        "yuhuili",
        "Llama-3",
        "vibesys-model-meta-llama",
        "vibesys-model-yuhuili",
    )
    for token in forbidden:
        assert token not in notes, f"prompt_notes leaks task-specific token {token!r}"


def test_modal_environment_per_run_namespace_prefix_unique(tmp_path):
    """Two runs (different exp_dir names) must produce different Modal
    namespace prefixes so concurrent runs cannot collide on app names,
    web-endpoint labels, or auxiliary volumes."""
    backend_a = FakeBackend()
    backend_b = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    ws_a = tmp_path / "20260429-100000-runA" / "workspace"
    ws_a.mkdir(parents=True)
    ws_b = tmp_path / "20260429-100100-runB" / "workspace"
    ws_b.mkdir(parents=True)

    log_a = tmp_path / "logsA"
    log_a.mkdir(exist_ok=True)
    log_b = tmp_path / "logsB"
    log_b.mkdir(exist_ok=True)

    req_a = RunEnvironmentRequest(
        log_dir=log_a,
        workspace=ws_a,
        ref_dir=None,
        backend=backend_a,
        agent_backend="cli",
        cli_provider="codex",
    )
    req_b = RunEnvironmentRequest(
        log_dir=log_b,
        workspace=ws_b,
        ref_dir=None,
        backend=backend_b,
        agent_backend="cli",
        cli_provider="codex",
    )
    notes_a = env.open(req_a).view.prompt_notes
    notes_b = env.open(req_b).view.prompt_notes

    # Each run's prefix is `vibesys-<exp-dir-name-sanitized>`.
    assert "vibesys-20260429-100000-runa" in notes_a
    assert "vibesys-20260429-100100-runb" in notes_b
    assert "vibesys-20260429-100000-runa" not in notes_b
    assert "vibesys-20260429-100100-runb" not in notes_a


def test_modal_environment_runtime_notes_describe_profile_contract(tmp_path):
    """The runtime notes must spell out the modal_profile / profile_remote
    contract; without it the profiler agent has no Modal entrypoint to
    invoke and falls back to local synthetic-weight profiling."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="cli", cli_provider="codex"))
    notes = session.view.prompt_notes

    assert "modal_profile" in notes
    assert "profile_remote" in notes
    assert "@app.local_entrypoint()" in notes
    assert "torch.profiler" in notes
    # Schema reference for the analyzer-compatible JSON shape.
    assert "analyze_torch_profile.py" in notes
    assert "total_cuda_time_us" in notes


def test_modal_environment_with_deepagents_uses_docker_too(tmp_path):
    """The deepagents path also runs locally in Docker now — Modal is a
    dispatch target, not a runtime for the agent."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(_request(tmp_path, backend, agent_backend="deepagents"))

    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert session.view.cli_modal_sandboxed is False


def test_unknown_environment_name_raises():
    with pytest.raises(ValueError, match="unknown run environment"):
        build_run_environment(RunEnvironmentSpec("wat"))


def test_docker_remove_workspace_child_quotes_path(tmp_path, monkeypatch):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    ok = env.remove_workspace_child(
        tmp_path,
        "semi;touch hacked",
        backend=backend,
    )

    assert ok is True
    shell_command = calls[0][-1]
    assert "rm -rf -- " in shell_command
    assert "'/workspace/semi;touch hacked'" in shell_command


def test_modal_teardown_deployment_stops_app_via_cli(monkeypatch):
    import sys as _sys

    env = build_run_environment(RunEnvironmentSpec("modal"))
    calls = []
    logs = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)

    assert calls == [[_sys.executable, "-m", "modal", "app", "stop", "vibesys-run-g1c2", "--yes"]]
    assert any("stopped candidate app vibesys-run-g1c2" in line for line in logs)


def test_modal_teardown_deployment_is_best_effort_on_nonzero(monkeypatch):
    env = build_run_environment(RunEnvironmentSpec("modal"))
    logs = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "boom"
        return result

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    # Must not raise.
    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)
    assert any("failed" in line for line in logs)


def test_modal_teardown_deployment_is_best_effort_on_exception(monkeypatch):
    env = build_run_environment(RunEnvironmentSpec("modal"))
    logs = []

    def fake_run(cmd, **kwargs):
        raise TimeoutError("stuck")

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fake_run)

    env.teardown_deployment("vibesys-run-g1c2", log=logs.append)
    assert any("raised" in line for line in logs)


@pytest.mark.parametrize("name", ["local", "docker"])
def test_non_modal_teardown_deployment_is_noop(name, monkeypatch):
    env = build_run_environment(RunEnvironmentSpec(name))

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for non-Modal envs")

    monkeypatch.setattr("vibesys.sandbox.run_environment.subprocess.run", fail_run)

    # No deployment to stop — must be a silent no-op.
    env.teardown_deployment("vibesys-run-g1c2", log=lambda _: None)
