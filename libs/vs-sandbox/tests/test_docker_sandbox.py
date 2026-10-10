"""Tests for DockerSandbox against scripted and in-memory Docker CLIs; no Docker required."""

import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Literal

import pytest

from vs_sandbox.api import (
    BeforeReadyContext,
    CommandRunner,
    SandboxLifecycleError,
    SandboxLifecycleHooks,
)
from vs_sandbox.api.testing import (
    DockerCliOutcome,
    FakeDockerEngine,
    ScriptedDockerCli,
    docker_result,
    docker_timed_out,
)
from vs_sandbox.docker_sandbox import (
    AGENT_HOME,
    DockerSandbox,
    _cleanup_containers,
    _first_component_below,
    _live_containers,
)
from vs_sandbox.host_resources import HostResource, HostResourceAccess


class _RecordingHooks(SandboxLifecycleHooks):
    def __init__(self, invocations: list[CommandRunner]) -> None:
        self._invocations = invocations

    def before_ready(self, context: BeforeReadyContext) -> None:
        self._invocations.append(context.sandbox)


class _FailingHooks(SandboxLifecycleHooks):
    def before_ready(self, context: BeforeReadyContext) -> None:
        del context
        _failure_message = "setup exploded"
        raise ValueError(_failure_message)


@pytest.fixture
def docker() -> ScriptedDockerCli:
    return ScriptedDockerCli()


@pytest.fixture
def sandbox(tmp_path: Path, docker: ScriptedDockerCli) -> DockerSandbox:
    return DockerSandbox(
        docker=docker,
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
    )


@pytest.fixture
def sandbox_with_mounts(tmp_path: Path, docker: ScriptedDockerCli) -> DockerSandbox:
    return DockerSandbox(
        docker=docker,
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
        bind_mounts=[
            (str(tmp_path / "model_weights"), "/workspace/reference/model", True),
            (str(tmp_path / "accuracy_checker"), "/workspace/accuracy_checker", True),
        ],
    )


def _start_test_container(
    sandbox: DockerSandbox,
    docker: ScriptedDockerCli,
    container_id: str = "abc123",
) -> None:
    """Start *sandbox* with a fake Docker id, then forget the script and setup calls."""
    docker.always(docker_result(stdout=f"{container_id}\n"))
    sandbox.start()
    docker.clear_script()
    docker.calls.clear()


def _failed_cleanup(mode: Literal["raises", "nonzero"]) -> DockerCliOutcome:
    """A stop or remove that either throws like a dead daemon or exits nonzero."""
    if mode == "raises":
        return OSError("Docker daemon disconnected")
    return docker_result(returncode=1, stderr="daemon unavailable")


def _assert_container_stopped(sandbox: DockerSandbox) -> None:
    with pytest.raises(RuntimeError, match="no running container"):
        _ = sandbox.container_id


def _read_sandbox_metadata(workspace: Path) -> dict[str, object]:
    metadata_path = workspace / ".docker_metadata.json"
    return json.loads(metadata_path.read_text())


class TestStart:
    def test_start_runs_docker_run_with_correct_args(
        self,
        sandbox: DockerSandbox,
        docker: ScriptedDockerCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Remove CUDA_VISIBLE_DEVICES so fallback to "all" is tested
        monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

        docker.always(docker_result(stdout="abc123container\n"))

        sandbox.start()

        # First call: docker run
        docker_run_call = docker.calls[0]
        cmd = docker_run_call.argv
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "-d" in cmd
        assert "--gpus" in cmd
        idx = cmd.index("--gpus")
        assert cmd[idx + 1] == "all"
        assert "--workdir" in cmd
        assert "/workspace" in cmd
        assert "nvcr.io/nvidia/pytorch:25.04-py3" in cmd
        assert "sleep" in cmd
        assert "infinity" in cmd
        # Container should be named with vibesys prefix
        assert "--name" in cmd
        name_idx = cmd.index("--name")
        assert cmd[name_idx + 1].startswith("vibesys-")
        assert docker_run_call.timeout_seconds == 120

    def test_start_docker_run_timeout_raises_clear_error(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_timed_out(["docker", "run"], 120))

        with pytest.raises(RuntimeError, match="Timed out starting Docker container"):
            sandbox.start()

    def test_start_failure_removes_created_container(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        docker.then(
            docker_result(returncode=125, stdout="abc123container\n", stderr="gpu error"),
            docker_result(),
            docker_result(),
        )

        with pytest.raises(RuntimeError, match="Failed to start Docker container"):
            sandbox.start()

        stop_call, rm_call = docker.calls[1:]
        assert stop_call.argv == ("docker", "stop", "abc123container")
        assert stop_call.timeout_seconds == 30
        assert rm_call.argv == ("docker", "rm", "-f", "abc123container")
        assert rm_call.timeout_seconds == 10
        _assert_container_stopped(sandbox)
        assert "abc123container" not in _live_containers

    def test_start_failure_retains_created_container_when_removal_fails(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        docker.then(
            docker_result(returncode=125, stdout="abc123container\n", stderr="gpu error"),
            docker_result(),
            docker_result(returncode=1, stderr="daemon unavailable"),
        )

        try:
            with pytest.raises(RuntimeError, match="Failed to start Docker container"):
                sandbox.start()

            assert sandbox.container_id == "abc123container"
            assert "abc123container" in _live_containers
        finally:
            _live_containers.pop("abc123container", None)

    def test_start_uses_first_cuda_visible_device(
        self, sandbox: DockerSandbox, monkeypatch: pytest.MonkeyPatch, docker: ScriptedDockerCli
    ) -> None:
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3,5,7")
        docker.always(docker_result(stdout="abc123container\n"))

        sandbox.start()

        cmd = docker.argvs[0]
        idx = cmd.index("--gpus")
        assert cmd[idx + 1] == "device=3"
        # DockerSandbox no longer hardcodes CUDA_VISIBLE_DEVICES; the cuda
        # backend supplies it via env=. The shape was tested above.

    def test_start_bind_mounts_workspace(
        self, sandbox: DockerSandbox, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd = docker.argvs[0]
        # Should have -v for workspace mount
        cmd_str = " ".join(cmd)
        assert f"{tmp_path / 'workspace'}:/workspace" in cmd_str

    def test_start_bind_mounts_extra(
        self, sandbox_with_mounts: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123\n"))

        sandbox_with_mounts.start()

        cmd = docker.argvs[0]
        cmd_str = " ".join(cmd)
        # Extra bind mounts should be read-only
        assert "/workspace/reference/model:ro" in cmd_str
        assert "/workspace/accuracy_checker:ro" in cmd_str

    def test_no_install_step_runs_at_start(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        """The agent image ships every tool baked in; start() installs nothing."""
        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd_strs = [" ".join(argv) for argv in docker.argvs]
        assert not any("pip install" in cmd for cmd in cmd_strs)
        assert not any("apt-get" in cmd for cmd in cmd_strs)

    def test_init_failure_stops_and_removes_created_container(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:

        invocations: list[CommandRunner] = []
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1234,
            agent_gid=5678,
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            # Current agent user ids, mismatched, so a remap is attempted.
            docker_result(stdout="1000\n1000\n"),
            docker_result(returncode=17, stdout="partial output", stderr="usermod failed"),
            docker_result(),
            docker_result(),
        )

        try:
            with pytest.raises(RuntimeError, match="agent user id remap failed"):
                sandbox.start()

            _assert_container_stopped(sandbox)
            assert "abc123" not in _live_containers
            assert invocations == []
            assert docker.argvs[-2] == ("docker", "stop", "abc123")
            assert docker.argvs[-1] == ("docker", "rm", "-f", "abc123")
        finally:
            _live_containers.pop("abc123", None)


class TestAgentUserRemap:
    def test_remaps_agent_user_when_ids_differ(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=4242,
            agent_gid=4343,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            # Image default agent user is 1000:1000.
            docker_result(stdout="1000\n1000\n"),
            docker_result(),
        )

        sandbox.start()

        cmd = docker.argvs[2]
        assert cmd[:4] == ("docker", "exec", "-u", "root")
        cmd_str = " ".join(cmd)
        assert "usermod -o -u 4242 agent" in cmd_str
        assert "groupmod -o -g 4343 agent" in cmd_str
        assert "chown -R agent:agent /home/agent" in cmd_str

    def test_skips_remap_when_ids_already_match(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1000,
            agent_gid=1000,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            docker_result(stdout="1000\n1000\n"),
        )

        sandbox.start()

        # Only the id query follows `docker run`; no usermod/groupmod exec.
        assert len(docker.calls) == 2
        assert not any("usermod" in " ".join(argv) for argv in docker.argvs)

    def test_remap_runs_as_root_but_agent_commands_do_not(self, tmp_path: Path) -> None:
        (tmp_path / "workspace").mkdir()
        (tmp_path / "engine").mkdir()
        engine = FakeDockerEngine(tmp_path / "engine", agent_ids=(1000, 1000))
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=4242,
            agent_gid=4343,
            docker=engine,
        )
        sandbox.start()
        try:
            sandbox.execute("echo hi")

            execs = [call for call in engine.calls if call[1] == "exec"]
            assert any(call[2:4] == ("-u", "root") and "usermod" in call[-1] for call in execs)
            assert "-u" not in execs[-1]
        finally:
            sandbox.stop()


class TestAuthFileCopy:
    def test_copies_staged_files_into_agent_home_and_chowns_them(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1000,
            agent_gid=1000,
            auth_files=[("/opt/vibesys-auth/0", "/home/agent/.claude.json")],
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            docker_result(stdout="1000\n1000\n"),
            docker_result(),
        )

        sandbox.start()

        cmd = docker.argvs[2]
        assert cmd[:4] == ("docker", "exec", "-u", "root")
        cmd_str = " ".join(cmd)
        assert "cp -a /opt/vibesys-auth/0 /home/agent/.claude.json" in cmd_str
        assert "find /home/agent/.claude.json -xdev -exec chown -h agent:agent" in cmd_str

    def test_no_auth_files_by_default(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        assert not any("cp -a" in " ".join(argv) for argv in docker.argvs)


class TestExecute:
    """Docker-specific execution facts; the result contract is in test_sandbox_contract.py."""

    @staticmethod
    def _started(tmp_path: Path) -> tuple[DockerSandbox, FakeDockerEngine]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "engine").mkdir()
        engine = FakeDockerEngine(tmp_path / "engine", agent_ids=(os.getuid(), os.getgid()))
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            agent_uid=os.getuid(),
            agent_gid=os.getgid(),
            docker=engine,
        )
        sandbox.start()
        return sandbox, engine

    def test_execute_runs_bash_in_the_workspace_through_docker_exec(self, tmp_path: Path) -> None:
        sandbox, engine = self._started(tmp_path)
        try:
            result = sandbox.execute("pwd -P && echo hello > made-by-exec")
            exec_calls = [call for call in engine.calls if call[1] == "exec"][-1]

            assert result.exit_code == 0
            assert result.stdout == f"{(tmp_path / 'workspace').resolve()}\n"
            assert (tmp_path / "workspace" / "made-by-exec").read_text() == "hello\n"
            assert exec_calls[exec_calls.index("-w") + 1] == "/workspace"
            assert exec_calls[-3:-1] == ("bash", "-c")
        finally:
            sandbox.stop()

    def test_a_container_removed_underneath_reports_the_daemon_error(self, tmp_path: Path) -> None:
        sandbox, engine = self._started(tmp_path)
        engine.run(("docker", "rm", "-f", sandbox.container_id), timeout_seconds=1)

        result = sandbox.execute("echo hello")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "No such container" in result.stderr

    def test_execute_without_start_raises(self, sandbox: DockerSandbox) -> None:
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.execute("echo hello")


class TestLifecycleHooks:
    def test_hooks_run_before_ready(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        docker.always(docker_result(stdout="abc123container\n"))
        invocations: list[CommandRunner] = []

        s = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        s.start()
        assert invocations == [s]

    def test_hooks_re_run_on_restart(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        """A second start, such as device reselection, reruns the hooks."""
        docker.always(docker_result(stdout="abc123container\n"))
        invocations: list[CommandRunner] = []

        s = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        s.start()
        s.start()
        assert invocations == [s, s]

    def test_setup_failure_preserves_error_when_stop_fails(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:

        docker.on(("docker", "run"), docker_result(stdout="abc123\n"))
        docker.on(("docker", "stop"), OSError("Docker daemon disconnected"))
        docker.always(docker_result())
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            lifecycle_hooks=[_FailingHooks()],
        )

        try:
            with pytest.raises(SandboxLifecycleError, match="_FailingHooks failed") as error:
                sandbox.start()

            assert isinstance(error.value.__cause__, ValueError)
            _assert_container_stopped(sandbox)
            assert "abc123" not in _live_containers
            assert ("docker", "stop", "abc123") in docker.argvs
            assert ("docker", "rm", "-f", "abc123") in docker.argvs
        finally:
            _live_containers.pop("abc123", None)

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    def test_setup_failure_retains_container_for_retry_when_removal_fails(
        self, cleanup_mode: Literal["raises", "nonzero"], tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:

        docker.on(("docker", "run"), docker_result(stdout="abc123\n"))
        for cleanup in (("docker", "stop"), ("docker", "rm")):
            docker.on(cleanup, _failed_cleanup(cleanup_mode))
        docker.always(docker_result())
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            lifecycle_hooks=[_FailingHooks()],
        )

        try:
            with pytest.raises(SandboxLifecycleError, match="_FailingHooks failed"):
                sandbox.start()

            assert sandbox.container_id == "abc123"
            assert "abc123" in _live_containers
        finally:
            _live_containers.pop("abc123", None)


class TestStop:
    def test_stop_removes_container(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123container\n"))
        sandbox.start()
        docker.calls.clear()

        docker.always(docker_result())
        sandbox.stop()

        # Should call docker stop then docker rm
        assert len(docker.calls) == 2
        stop_cmd = docker.argvs[0]
        rm_cmd = docker.argvs[1]
        assert stop_cmd[0] == "docker"
        assert "stop" in stop_cmd
        assert rm_cmd[0] == "docker"
        assert "rm" in rm_cmd

    def test_stop_idempotent(self, sandbox: DockerSandbox, docker: ScriptedDockerCli) -> None:
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        docker.calls.clear()

        docker.always(docker_result())
        sandbox.stop()
        docker.calls.clear()

        # Second stop should be a no-op
        sandbox.stop()
        assert len(docker.calls) == 0

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    def test_failed_removal_retains_ownership_and_can_be_retried(
        self,
        cleanup_mode: Literal["raises", "nonzero"],
        sandbox: DockerSandbox,
        docker: ScriptedDockerCli,
    ) -> None:

        _start_test_container(sandbox, docker)

        docker.on(("docker", "rm"), _failed_cleanup(cleanup_mode))
        docker.always(docker_result())
        try:
            expected_error = OSError if cleanup_mode == "raises" else RuntimeError
            with pytest.raises(expected_error):
                sandbox.stop()

            assert sandbox.container_id == "abc123"
            assert "abc123" in _live_containers

            docker.clear_script()
            docker.always(docker_result())
            sandbox.stop()

            _assert_container_stopped(sandbox)
            assert "abc123" not in _live_containers
        finally:
            _live_containers.pop("abc123", None)

    def test_stop_failure_does_not_prevent_forced_removal(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        _start_test_container(sandbox, docker)
        docker.then(
            OSError("Docker daemon disconnected"),
            docker_result(),
        )

        sandbox.stop()

        assert docker.argvs[1] == ("docker", "rm", "-f", "abc123")
        _assert_container_stopped(sandbox)
        assert "abc123" not in _live_containers

    def test_already_absent_container_clears_ownership(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        _start_test_container(sandbox, docker)
        docker.always(docker_result(returncode=1, stderr="Error: No such container: abc123"))

        sandbox.stop()

        _assert_container_stopped(sandbox)
        assert "abc123" not in _live_containers

    def test_removal_already_in_progress_clears_ownership(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        _start_test_container(sandbox, docker)
        docker.always(
            docker_result(
                returncode=1,
                stderr=(
                    "Error response from daemon: removal of container abc123 is already in progress"
                ),
            )
        )

        sandbox.stop()

        _assert_container_stopped(sandbox)
        assert "abc123" not in _live_containers

    def test_keyboard_interrupt_is_not_swallowed(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        _start_test_container(sandbox, docker)
        docker.always(KeyboardInterrupt())

        try:
            with pytest.raises(KeyboardInterrupt):
                sandbox.stop()

            assert len(docker.calls) == 1
            assert sandbox.container_id == "abc123"
            assert "abc123" in _live_containers
        finally:
            _live_containers.pop("abc123", None)


class TestIdProperty:
    def test_id_property(self, sandbox: DockerSandbox, docker: ScriptedDockerCli) -> None:
        docker.always(docker_result(stdout="abc123def456ghi789\n"))
        sandbox.start()

        assert sandbox.id.startswith("vibesys-")
        assert len(sandbox.id) > len("vibesys-")


class TestContainerIdProperty:
    def test_container_id_returns_running_container(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123def456ghi789\n"))
        sandbox.start()

        assert sandbox.container_id == "abc123def456ghi789"

    def test_container_id_before_start_raises(self, sandbox: DockerSandbox) -> None:
        with pytest.raises(RuntimeError, match="no running container"):
            _ = sandbox.container_id

    def test_container_id_after_stop_raises(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:
        docker.always(docker_result(stdout="abc123def456ghi789\n"))
        sandbox.start()
        sandbox.stop()

        with pytest.raises(RuntimeError, match="no running container"):
            _ = sandbox.container_id


class TestContextManager:
    def test_context_manager(self, sandbox: DockerSandbox, docker: ScriptedDockerCli) -> None:
        docker.always(docker_result(stdout="abc123\n"))

        with sandbox:
            assert sandbox.container_id

        # After exit, container should be stopped
        _assert_container_stopped(sandbox)


class TestCleanupOnExit:
    @pytest.fixture(autouse=True)
    def _clear_live_containers(self) -> Generator[None, None, None]:
        """Isolate the global _live_containers registry between tests."""

        _live_containers.clear()
        yield
        _live_containers.clear()

    def test_live_containers_tracked(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        assert sandbox.container_id in _live_containers

        sandbox.stop()
        assert "abc123" not in _live_containers

    def test_cleanup_containers_stops_all(
        self, sandbox: DockerSandbox, docker: ScriptedDockerCli
    ) -> None:

        docker.always(docker_result(stdout="container_xyz\n"))
        sandbox.start()
        assert "container_xyz" in _live_containers

        docker.calls.clear()
        docker.always(docker_result())

        _cleanup_containers(docker)

        assert len(_live_containers) == 0
        # Should have called docker stop + docker rm
        stop_calls = [argv for argv in docker.argvs if "stop" in argv]
        rm_calls = [argv for argv in docker.argvs if "rm" in argv]
        assert len(stop_calls) == 1
        assert len(rm_calls) == 1

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    def test_cleanup_retains_failed_removal_for_retry(
        self, cleanup_mode: Literal["raises", "nonzero"], docker: ScriptedDockerCli
    ) -> None:

        _live_containers["abc123"] = "vibesys-test"

        docker.on(("docker", "rm"), _failed_cleanup(cleanup_mode))
        docker.always(docker_result())

        _cleanup_containers(docker)

        assert "abc123" in _live_containers
        rm_call = docker.calls[1]
        assert rm_call.argv == ("docker", "rm", "-f", "abc123")
        assert rm_call.timeout_seconds == 10

    def test_cleanup_forces_removal_after_stop_exception(self, docker: ScriptedDockerCli) -> None:

        _live_containers["abc123"] = "vibesys-test"
        docker.then(
            OSError("Docker daemon disconnected"),
            docker_result(),
        )

        _cleanup_containers(docker)

        assert docker.argvs[1] == ("docker", "rm", "-f", "abc123")
        assert "abc123" not in _live_containers


class TestEnvVars:
    def test_env_vars_passed_to_docker_run(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="pytorch:latest",
            env={"MY_KEY": "my_value", "OTHER": "thing"},
        )

        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd = docker.argvs[0]
        cmd_str = " ".join(cmd)
        assert "-e" in cmd
        assert "MY_KEY=my_value" in cmd_str
        assert "OTHER=thing" in cmd_str

    def test_credential_values_reach_the_container_but_not_the_log(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        log_path = tmp_path / "docker.log"
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="pytorch:latest",
            env={
                "ANTHROPIC_AUTH_TOKEN": "sk-secret-token",
                "ANTHROPIC_BASE_URL": "https://proxy.invalid/v1",
                "PYTHONPATH": "/opt/vibesys",
            },
            log_path=log_path,
        )

        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd_str = " ".join(docker.argvs[0])
        assert "ANTHROPIC_AUTH_TOKEN=sk-secret-token" in cmd_str

        log_text = log_path.read_text()
        assert "sk-secret-token" not in log_text
        assert "ANTHROPIC_AUTH_TOKEN=<redacted>" in log_text
        # Endpoint selection is a diagnostic, not a credential.
        assert "ANTHROPIC_BASE_URL=https://proxy.invalid/v1" in log_text
        assert "PYTHONPATH=/opt/vibesys" in log_text

    def test_credential_values_are_omitted_from_workspace_metadata(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="pytorch:latest",
            env={"ANTHROPIC_API_KEY": "sk-secret-key", "PYTHONPATH": "/opt/vibesys"},
        )

        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        metadata_text = (workspace / ".docker_metadata.json").read_text()
        assert "sk-secret-key" not in metadata_text
        assert json.loads(metadata_text)["env"] == {"PYTHONPATH": "/opt/vibesys"}

    def test_start_failure_error_does_not_expose_credentials(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="pytorch:latest",
            env={"ANTHROPIC_AUTH_TOKEN": "sk-secret-token"},
        )

        docker.always(docker_result(returncode=125, stderr="boom"))

        with pytest.raises(RuntimeError) as excinfo:
            sandbox.start()

        assert "sk-secret-token" not in str(excinfo.value)
        assert "ANTHROPIC_AUTH_TOKEN=<redacted>" in str(excinfo.value)


class TestDevicePassthrough:
    def test_devices_emit_device_flags_and_no_gpus(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="public.ecr.aws/neuron/pytorch-inference-neuronx:latest",
            gpus=None,  # Neuron uses --device, not --gpus
            devices=["/dev/neuron0", "/dev/neuron1"],
        )
        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd = docker.argvs[0]
        assert "--gpus" not in cmd
        # Each device forwarded with its own --device flag.
        assert cmd.count("--device") == 2
        for dev in ("/dev/neuron0", "/dev/neuron1"):
            i = cmd.index(dev)
            assert cmd[i - 1] == "--device"
        assert _read_sandbox_metadata(workspace)["devices"] == ["/dev/neuron0", "/dev/neuron1"]

    def test_group_add_emits_group_add_flags(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        """AMD /dev/kfd and /dev/dri/* are group-owned; without --group-add the
        container user cannot open them and every HIP call fails at runtime."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="rocm/pytorch:latest",
            gpus=None,
            devices=["/dev/kfd", "/dev/dri/renderD128"],
            group_add=["video", "render"],
        )
        docker.always(docker_result(stdout="abc123\n"))

        sandbox.start()

        cmd = docker.argvs[0]
        assert cmd.count("--group-add") == 2
        for group in ("video", "render"):
            i = cmd.index(group)
            assert cmd[i - 1] == "--group-add"
        # Recorded so a reattaching shell reconstructs the same device access;
        # without it the container user cannot open /dev/kfd and every HIP call
        # fails — the exact failure --group-add exists to prevent.
        assert _read_sandbox_metadata(workspace)["group_add"] == ["video", "render"]

    def test_no_group_add_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        assert "--group-add" not in cmd

    def test_no_devices_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        assert "--device" not in cmd


class TestEntrypointOverride:
    def test_entrypoint_override_emitted_before_image(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        image = "public.ecr.aws/neuron/pytorch-inference-neuronx:latest"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image=image,
            gpus=None,
            entrypoint="",  # clear the DLC's baked-in model-server entrypoint
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        assert "--entrypoint" in cmd
        ep_idx = cmd.index("--entrypoint")
        assert cmd[ep_idx + 1] == ""
        # Override must precede the image positional, which precedes the command.
        img_idx = cmd.index(image)
        assert ep_idx < img_idx
        assert cmd[-2:] == ("sleep", "infinity")
        assert _read_sandbox_metadata(workspace)["entrypoint"] == ""

    def test_no_entrypoint_flag_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        assert "--entrypoint" not in cmd


class TestShmSize:
    def test_shm_size_emitted(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="img",
            shm_size="16g",
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        assert "--shm-size" in cmd
        assert cmd[cmd.index("--shm-size") + 1] == "16g"
        assert _read_sandbox_metadata(workspace)["shm_size"] == "16g"

    def test_no_shm_size_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker, host_workspace=str(tmp_path / "workspace"), image="img"
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        assert "--shm-size" not in docker.argvs[0]


class TestAutoRemove:
    def test_auto_remove_emits_rm_flag(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker, host_workspace=str(tmp_path / "workspace"), image="img", auto_remove=True
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        assert "--rm" in docker.argvs[0]

    def test_no_rm_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker, host_workspace=str(tmp_path / "workspace"), image="img"
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        assert "--rm" not in docker.argvs[0]


class TestWritableCredentialMounts:
    """A credential mounted writable into HOME is the host's file, not ours to chown."""

    @staticmethod
    def _root_scripts(engine: FakeDockerEngine) -> list[str]:
        return [
            call[-1]
            for call in engine.calls
            if call[1] == "exec" and call[2:4] == ("-u", "root") and call[-2] == "-c"
        ]

    def _start(
        self,
        tmp_path: Path,
        bind_mounts: list[tuple[str, str, bool]],
        auth_files: list[tuple[str, str]] | None = None,
    ) -> FakeDockerEngine:
        (tmp_path / "engine").mkdir()
        engine = FakeDockerEngine(tmp_path / "engine")
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            # The engine's agent already has these ids, so no remap script runs
            # and the host user's uid cannot change which scripts are recorded.
            agent_uid=1000,
            agent_gid=1000,
            docker=engine,
            bind_mounts=bind_mounts,
            auth_files=auth_files,
        )
        sandbox.start()
        sandbox.stop()
        return engine

    def test_mounts_the_file_writable_and_hands_its_parent_directory_to_the_agent(
        self, tmp_path: Path
    ) -> None:
        credential = tmp_path / "auth.json"
        credential.write_text("{}")

        engine = self._start(tmp_path, [(str(credential), "/home/agent/.codex/auth.json", False)])

        run = next(call for call in engine.calls if call[1] == "run")
        assert f"{credential}:/home/agent/.codex/auth.json" in run
        assert "chown agent:agent /home/agent/.codex" in self._root_scripts(engine)

    def test_never_recurses_a_chown_into_a_mounted_credential(self, tmp_path: Path) -> None:
        credential = tmp_path / "auth.json"
        credential.write_text("{}")

        engine = self._start(
            tmp_path,
            [(str(credential), "/home/agent/.codex/auth.json", False)],
            auth_files=[("/opt/vibesys-auth/0", "/home/agent/.codex/config.toml")],
        )

        copy = next(script for script in self._root_scripts(engine) if "cp -a" in script)
        assert "! -path /home/agent/.codex/auth.json" in copy
        assert "chown -R" not in copy

    def test_read_only_mounts_need_no_ownership_step(self, tmp_path: Path) -> None:
        engine = self._start(tmp_path, [(str(tmp_path), "/home/agent/.codex/config.toml", True)])

        assert not any("chown" in script for script in self._root_scripts(engine))


class TestAuthCopyOwnership:
    def test_a_nested_auth_file_chowns_its_top_directory(self) -> None:
        assert _first_component_below("/home/agent", "/home/agent/.codex/auth.json") == (
            "/home/agent/.codex"
        )
        assert _first_component_below("/home/agent", "/home/agent/.claude.json") == (
            "/home/agent/.claude.json"
        )
        assert (
            _first_component_below("/home/agent", "/home/agent/.config/opencode/opencode.json")
            == "/home/agent/.config"
        )


class TestResources:
    """Constructing from a ``HostResource`` list, as ``WorkspaceSandbox`` does."""

    def test_read_only_resource_mounts_ro(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(
                HostResource(tmp_path / "toolchain", HostResourceAccess.READ_ONLY, "toolchain"),
            ),
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        cmd_str = " ".join(docker.argvs[0])
        assert f"{tmp_path / 'toolchain'}:{tmp_path / 'toolchain'}:ro" in cmd_str

    def test_read_write_resource_mounts_without_ro_suffix(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(HostResource(tmp_path / "state", HostResourceAccess.READ_WRITE, "state"),),
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        cmd = docker.argvs[0]
        cmd_str = " ".join(cmd)
        mount = f"{tmp_path / 'state'}:{tmp_path / 'state'}"
        assert mount in cmd_str
        assert f"{mount}:ro" not in cmd_str

    def test_agent_path_becomes_the_mount_destination(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        resource_path = tmp_path / "toolchain"
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(
                HostResource(
                    resource_path,
                    HostResourceAccess.READ_ONLY,
                    "toolchain",
                    agent_path="/opt/vibesys-toolchain",
                ),
            ),
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        cmd_str = " ".join(docker.argvs[0])
        assert f"{resource_path}:/opt/vibesys-toolchain:ro" in cmd_str

    def test_unlisted_path_is_never_mounted(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(HostResource(tmp_path / "listed", HostResourceAccess.READ_ONLY, "listed"),),
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        cmd_str = " ".join(docker.argvs[0])
        assert str(tmp_path / "unlisted") not in cmd_str

    def test_resources_combine_with_explicit_bind_mounts(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        """Existing callers that pass bind_mounts directly keep working unchanged."""
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            bind_mounts=[(str(tmp_path / "explicit"), "/explicit", True)],
            resources=(
                HostResource(tmp_path / "declared", HostResourceAccess.READ_ONLY, "declared"),
            ),
        )
        docker.always(docker_result(stdout="abc\n"))
        sandbox.start()
        cmd_str = " ".join(docker.argvs[0])
        assert f"{tmp_path / 'explicit'}:/explicit:ro" in cmd_str
        assert f"{tmp_path / 'declared'}:{tmp_path / 'declared'}:ro" in cmd_str

    def test_no_resources_by_default(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker, host_workspace=str(tmp_path / "workspace"), image="img"
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()
        command = docker.argvs[0]
        mount_arguments = [
            command[index + 1] for index, argument in enumerate(command[:-1]) if argument == "-v"
        ]
        assert mount_arguments == [f"{tmp_path / 'workspace'}:/workspace"]


class TestAgentPath:
    """``agent_path`` maps a host path to what the agent sees inside the container."""

    def test_workspace_path_maps_under_the_container_root(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(host_workspace=str(workspace), image="img")

        assert sandbox.agent_path(workspace) == "/workspace"
        assert sandbox.agent_path(workspace / "sub" / "file.py") == "/workspace/sub/file.py"

    def test_unset_agent_path_resource_maps_to_its_own_host_path(self, tmp_path: Path) -> None:
        resource_path = tmp_path / "toolchain"
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(HostResource(resource_path, HostResourceAccess.READ_ONLY, "toolchain"),),
        )

        assert sandbox.agent_path(resource_path / "bin" / "rustc") == str(
            resource_path / "bin" / "rustc"
        )

    def test_declared_agent_path_remaps_a_nested_path(self, tmp_path: Path) -> None:
        resource_path = tmp_path / "toolchain"
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            resources=(
                HostResource(
                    resource_path,
                    HostResourceAccess.READ_ONLY,
                    "toolchain",
                    agent_path="/opt/vibesys-toolchain",
                ),
            ),
        )

        assert (
            sandbox.agent_path(resource_path / "bin" / "rustc")
            == "/opt/vibesys-toolchain/bin/rustc"
        )

    def test_longest_prefix_wins_for_a_resource_nested_in_the_workspace(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        nested_resource = workspace / "vendor"
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image="img",
            resources=(
                HostResource(
                    nested_resource,
                    HostResourceAccess.READ_ONLY,
                    "vendor",
                    agent_path="/opt/vibesys-vendor",
                ),
            ),
        )

        assert sandbox.agent_path(nested_resource / "lib.so") == "/opt/vibesys-vendor/lib.so"
        assert sandbox.agent_path(workspace / "src" / "main.py") == "/workspace/src/main.py"

    def test_unrelated_path_is_identity(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")

        assert sandbox.agent_path("/etc/passwd") == "/etc/passwd"

    def test_normalizes_like_the_host_default(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")

        assert sandbox.agent_path("/foo//bar/") == "/foo/bar"
        assert sandbox.agent_path(Path("/foo/./bar")) == "/foo/bar"


class TestWrap:
    """``wrap`` builds the ``docker exec`` prefix a driver's command executor needs."""

    def test_wrap_before_start_raises(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.wrap(["echo", "hi"], str(tmp_path / "workspace"))

    def test_wrap_shape(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(docker=docker, host_workspace=str(workspace), image="img")
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        argv = sandbox.wrap(["echo", "hi"], workspace)

        assert argv == ["docker", "exec", "-i", "-w", "/workspace", "abc123", "echo", "hi"]

    def test_wrap_defaults_cwd_to_the_workspace_root(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        """Omitting cwd matches the base ``WorkspaceSandbox.wrap(argv)`` shape."""
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(docker=docker, host_workspace=str(workspace), image="img")
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        argv = sandbox.wrap(["echo", "hi"])

        assert argv == ["docker", "exec", "-i", "-w", "/workspace", "abc123", "echo", "hi"]

    def test_wrap_uses_agent_path_of_cwd(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(docker=docker, host_workspace=str(workspace), image="img")
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        argv = sandbox.wrap(["ls"], workspace / "sub")

        assert argv[4] == "/workspace/sub"

    def test_wrap_forwards_extra_env_as_dash_e_flags(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(workspace),
            image="img",
            env={"FOO": "bar"},
        )
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        argv = sandbox.wrap(["echo", "hi"], workspace)

        assert "-e" in argv
        assert "FOO=bar" in argv
        assert argv.index("-e") + 1 == argv.index("FOO=bar")

    def test_wrap_runs_as_the_image_default_user(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        """No ``-u`` flag: the container already runs as the remapped agent user."""
        workspace = tmp_path / "workspace"
        sandbox = DockerSandbox(docker=docker, host_workspace=str(workspace), image="img")
        docker.always(docker_result(stdout="abc123\n"))
        sandbox.start()

        argv = sandbox.wrap(["echo", "hi"], workspace)

        assert "-u" not in argv


class TestEnv:
    """``env`` reports HOME, the image's own PATH, and any extra env."""

    def test_env_before_start_raises(self, tmp_path: Path) -> None:
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")
        with pytest.raises(RuntimeError, match="not started"):
            _ = sandbox.env

    def test_env_reads_path_from_the_running_container(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            agent_uid=1000,
            agent_gid=1000,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            # Id query: already matches (agent_uid, agent_gid), so the remap
            # step is skipped and the very next call is the PATH read below.
            docker_result(stdout="1000\n1000\n"),
            docker_result(stdout="/usr/local/bin:/usr/bin:/bin\n"),
        )
        sandbox.start()

        env = sandbox.env

        assert env["HOME"] == AGENT_HOME
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        exec_cmd = docker.argvs[2]
        assert exec_cmd == ("docker", "exec", "abc123", "sh", "-c", "echo $PATH")

    def test_env_caches_path_across_calls(self, tmp_path: Path, docker: ScriptedDockerCli) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            agent_uid=1000,
            agent_gid=1000,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            docker_result(stdout="1000\n1000\n"),
            docker_result(stdout="/usr/bin:/bin\n"),
        )
        sandbox.start()

        first = sandbox.env
        second = sandbox.env

        assert first["PATH"] == second["PATH"] == "/usr/bin:/bin"
        # Only one PATH-reading exec call across both reads.
        path_reads = [argv for argv in docker.argvs if "echo $PATH" in " ".join(argv)]
        assert len(path_reads) == 1

    def test_extra_env_overrides_home_and_path(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            env={"HOME": "/custom/home", "EXTRA": "1"},
            agent_uid=1000,
            agent_gid=1000,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            docker_result(stdout="1000\n1000\n"),
            docker_result(stdout="/usr/bin\n"),
        )
        sandbox.start()

        env = sandbox.env

        assert env["HOME"] == "/custom/home"
        assert env["EXTRA"] == "1"
        assert env["PATH"] == "/usr/bin"

    def test_env_raises_when_path_cannot_be_read(
        self, tmp_path: Path, docker: ScriptedDockerCli
    ) -> None:
        sandbox = DockerSandbox(
            docker=docker,
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            agent_uid=1000,
            agent_gid=1000,
        )
        docker.then(
            docker_result(stdout="abc123\n"),
            docker_result(stdout="1000\n1000\n"),
            docker_result(returncode=1, stderr="no such container"),
        )
        sandbox.start()

        with pytest.raises(RuntimeError, match="could not read PATH"):
            _ = sandbox.env
