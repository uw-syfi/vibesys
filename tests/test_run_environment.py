from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibeserve_agent.backends import SandboxKind
from vibeserve_agent.sandbox.run_environment import (
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
            acc_checker_path="/host/acc",
            bench_path="/host/bench",
        )
    )

    assert backend.calls[0][0] is SandboxKind.LOCAL
    assert session.sandbox is backend.sandbox
    assert session.view.paths.acc_checker == "/host/acc"
    assert session.view.paths.bench == "/host/bench"
    assert session.view.isolated is False
    backend.sandbox.start.assert_not_called()


def test_docker_environment_opens_one_started_sandbox_with_agent_paths(tmp_path):
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("docker"))

    session = env.open(
        _request(
            tmp_path,
            backend,
            acc_checker_path="/host/acc",
            bench_path="/host/bench",
        )
    )

    assert backend.calls[0][0] is SandboxKind.DOCKER
    assert session.view.isolated is True
    assert session.view.cli_sandboxed is True
    assert session.view.paths.acc_checker == "acc_checker"
    assert session.view.paths.bench == "bench"
    backend.sandbox.start.assert_called_once()

    session.close()
    backend.sandbox.stop.assert_called_once()


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

    session = env.open(
        _request(tmp_path, backend, agent_backend="cli", cli_provider="codex")
    )

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

    session = env.open(
        _request(tmp_path, backend, agent_backend="cli", cli_provider="codex")
    )

    notes = session.view.prompt_notes
    assert "modal run" in notes
    assert "@app.cls" in notes or "@app.function" in notes
    assert "GPU" in notes
    # Tell the agent where to look up volume names rather than baking them in.
    assert "meta.json" in notes
    # No hardcoded model IDs or vibeserve-internal volume names should leak
    # into the runtime-notes block.
    forbidden = (
        "yuhuili",
        "Llama-3",
        "vibeserve-model-meta-llama",
        "vibeserve-model-yuhuili",
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
        log_dir=log_a, workspace=ws_a, ref_dir=None, backend=backend_a,
        agent_backend="cli", cli_provider="codex",
    )
    req_b = RunEnvironmentRequest(
        log_dir=log_b, workspace=ws_b, ref_dir=None, backend=backend_b,
        agent_backend="cli", cli_provider="codex",
    )
    notes_a = env.open(req_a).view.prompt_notes
    notes_b = env.open(req_b).view.prompt_notes

    # Each run's prefix is `vibeserve-<exp-dir-name-sanitized>`.
    assert "vibeserve-20260429-100000-runa" in notes_a
    assert "vibeserve-20260429-100100-runb" in notes_b
    assert "vibeserve-20260429-100000-runa" not in notes_b
    assert "vibeserve-20260429-100100-runb" not in notes_a


def test_modal_environment_runtime_notes_describe_profile_contract(tmp_path):
    """The runtime notes must spell out the modal_profile / profile_remote
    contract; without it the profiler agent has no Modal entrypoint to
    invoke and falls back to local synthetic-weight profiling."""
    backend = FakeBackend()
    env = build_run_environment(RunEnvironmentSpec("modal"))

    session = env.open(
        _request(tmp_path, backend, agent_backend="cli", cli_provider="codex")
    )
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

    monkeypatch.setattr("vibeserve_agent.sandbox.run_environment.subprocess.run", fake_run)

    ok = env.remove_workspace_child(
        tmp_path,
        "semi;touch hacked",
        backend=backend,
    )

    assert ok is True
    shell_command = calls[0][-1]
    assert "rm -rf -- " in shell_command
    assert "'/workspace/semi;touch hacked'" in shell_command
