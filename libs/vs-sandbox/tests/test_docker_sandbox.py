"""Tests for DockerSandbox — all mock subprocess.run, no Docker required."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vs_sandbox import BeforeReadyContext, SandboxLifecycleError, SandboxLifecycleHooks
from vs_sandbox.docker_sandbox import DockerSandbox, _first_component_below


class _RecordingHooks(SandboxLifecycleHooks):
    def __init__(self, invocations: list[object]) -> None:
        self._invocations = invocations

    def before_ready(self, context: BeforeReadyContext) -> None:
        self._invocations.append(context.sandbox)


class _FailingHooks(SandboxLifecycleHooks):
    def before_ready(self, context: BeforeReadyContext) -> None:  # noqa: ARG002
        raise ValueError("setup exploded")  # noqa: TRY003


@pytest.fixture
def sandbox(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    return DockerSandbox(
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
    )


@pytest.fixture
def sandbox_with_mounts(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    return DockerSandbox(
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
        bind_mounts=[
            (str(tmp_path / "model_weights"), "/workspace/reference/model", True),
            (str(tmp_path / "accuracy_checker"), "/workspace/accuracy_checker", True),
        ],
    )


class TestStart:
    @patch.dict("os.environ", {}, clear=False)
    @patch("subprocess.run")
    def test_start_runs_docker_run_with_correct_args(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        # Remove CUDA_VISIBLE_DEVICES so fallback to "all" is tested
        import os  # noqa: PLC0415  # tracked: #288

        os.environ.pop("CUDA_VISIBLE_DEVICES", None)

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr=""
        )

        sandbox.start()

        calls = mock_run.call_args_list
        # First call: docker run
        docker_run_call = calls[0]
        cmd = docker_run_call[0][0]
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
        assert docker_run_call.kwargs["timeout"] == 120

    @patch("subprocess.run")
    def test_start_docker_run_timeout_raises_clear_error(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["docker", "run"],
            timeout=120,
        )

        with pytest.raises(RuntimeError, match="Timed out starting Docker container"):
            sandbox.start()

    @patch("subprocess.run")
    def test_start_failure_removes_created_container(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=125,
                stdout="abc123container\n",
                stderr="gpu error",
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        with pytest.raises(RuntimeError, match="Failed to start Docker container"):
            sandbox.start()

        stop_call, rm_call = mock_run.call_args_list[1:]
        assert stop_call.args[0] == ["docker", "stop", "abc123container"]
        assert stop_call.kwargs["timeout"] == 30
        assert rm_call.args[0] == ["docker", "rm", "-f", "abc123container"]
        assert rm_call.kwargs["timeout"] == 10
        assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
        assert "abc123container" not in _live_containers

    @patch("subprocess.run")
    def test_start_failure_retains_created_container_when_removal_fails(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=125,
                stdout="abc123container\n",
                stderr="gpu error",
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="daemon unavailable"
            ),
        ]

        try:
            with pytest.raises(RuntimeError, match="Failed to start Docker container"):
                sandbox.start()

            assert sandbox._container_id == "abc123container"  # noqa: SLF001  # tracked: #288
            assert "abc123container" in _live_containers
        finally:
            _live_containers.pop("abc123container", None)
            sandbox._container_id = None  # noqa: SLF001  # tracked: #288

    @patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "3,5,7"})
    @patch("subprocess.run")
    def test_start_uses_first_cuda_visible_device(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        idx = cmd.index("--gpus")
        assert cmd[idx + 1] == "device=3"
        # DockerSandbox no longer hardcodes CUDA_VISIBLE_DEVICES; the cuda
        # backend supplies it via env=. The shape was tested above.

    @patch("subprocess.run")
    def test_start_bind_mounts_workspace(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        # Should have -v for workspace mount
        cmd_str = " ".join(cmd)
        assert f"{sandbox._host_workspace}:/workspace" in cmd_str  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_start_bind_mounts_extra(self, mock_run, sandbox_with_mounts):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox_with_mounts.start()

        cmd = mock_run.call_args_list[0][0][0]
        cmd_str = " ".join(cmd)
        # Extra bind mounts should be read-only
        assert "/workspace/reference/model:ro" in cmd_str
        assert "/workspace/accuracy_checker:ro" in cmd_str

    @patch("subprocess.run")
    def test_no_install_step_runs_at_start(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        """The agent image ships every tool baked in; start() installs nothing."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd_strs = [" ".join(c[0][0]) for c in mock_run.call_args_list]
        assert not any("pip install" in cmd for cmd in cmd_strs)
        assert not any("apt-get" in cmd for cmd in cmd_strs)

    @patch("subprocess.run")
    def test_init_failure_stops_and_removes_created_container(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        invocations: list[object] = []
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1234,
            agent_gid=5678,
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),
            # Current agent user ids, mismatched, so a remap is attempted.
            subprocess.CompletedProcess(args=[], returncode=0, stdout="1000\n1000\n", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=17, stdout="partial output", stderr="usermod failed"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        try:
            with pytest.raises(RuntimeError, match="agent user id remap failed"):
                sandbox.start()

            assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
            assert "abc123" not in _live_containers
            assert invocations == []
            assert mock_run.call_args_list[-2][0][0] == ["docker", "stop", "abc123"]
            assert mock_run.call_args_list[-1][0][0] == ["docker", "rm", "-f", "abc123"]
        finally:
            _live_containers.pop("abc123", None)


class TestAgentUserRemap:
    @patch("subprocess.run")
    def test_remaps_agent_user_when_ids_differ(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=4242,
            agent_gid=4343,
        )
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),
            # Image default agent user is 1000:1000.
            subprocess.CompletedProcess(args=[], returncode=0, stdout="1000\n1000\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        sandbox.start()

        remap_call = mock_run.call_args_list[2]
        cmd = remap_call[0][0]
        assert cmd[:4] == ["docker", "exec", "-u", "root"]
        cmd_str = " ".join(cmd)
        assert "usermod -o -u 4242 agent" in cmd_str
        assert "groupmod -o -g 4343 agent" in cmd_str
        assert "chown -R agent:agent /home/agent" in cmd_str

    @patch("subprocess.run")
    def test_skips_remap_when_ids_already_match(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1000,
            agent_gid=1000,
        )
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="1000\n1000\n", stderr=""),
        ]

        sandbox.start()

        # Only the id query follows `docker run`; no usermod/groupmod exec.
        assert mock_run.call_count == 2
        assert not any("usermod" in " ".join(c[0][0]) for c in mock_run.call_args_list)

    @patch("subprocess.run")
    def test_remap_runs_as_root_but_agent_commands_do_not(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=4242,
            agent_gid=4343,
        )
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="1000\n1000\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
        sandbox.start()
        mock_run.reset_mock()
        mock_run.side_effect = None
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hi\n", stderr=""
        )

        sandbox.execute("echo hi")

        exec_cmd = mock_run.call_args[0][0]
        assert "-u" not in exec_cmd


class TestAuthFileCopy:
    @patch("subprocess.run")
    def test_copies_staged_files_into_agent_home_and_chowns_them(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            agent_uid=1000,
            agent_gid=1000,
            auth_files=[("/opt/vibesys-auth/0", "/home/agent/.claude.json")],
        )
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="1000\n1000\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        sandbox.start()

        copy_call = mock_run.call_args_list[2]
        cmd = copy_call[0][0]
        assert cmd[:4] == ["docker", "exec", "-u", "root"]
        cmd_str = " ".join(cmd)
        assert "cp -a /opt/vibesys-auth/0 /home/agent/.claude.json" in cmd_str
        assert "chown -R agent:agent /home/agent/.claude.json" in cmd_str

    @patch("subprocess.run")
    def test_no_auth_files_by_default(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        assert not any("cp -a" in " ".join(c[0][0]) for c in mock_run.call_args_list)


class TestExecute:
    @patch("subprocess.run")
    def test_execute_runs_docker_exec(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        # Start first
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        # Execute command
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hello world\n", stderr=""
        )

        result = sandbox.execute("echo hello")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "exec" in cmd
        assert "-w" in cmd
        assert "/workspace" in cmd
        assert result.output == "hello world\n"
        assert result.exit_code == 0

    @patch("subprocess.run")
    def test_execute_timeout(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker exec", timeout=5)

        result = sandbox.execute("sleep 100", timeout=5)

        assert result.exit_code == -1
        assert "timed out" in result.output.lower()

    @patch("subprocess.run")
    def test_execute_output_truncation(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        # Set small max_output_bytes
        sandbox._max_output_bytes = 50  # noqa: SLF001  # tracked: #288
        big_output = "x" * 200
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=big_output, stderr=""
        )

        result = sandbox.execute("cat bigfile")

        assert result.truncated is True
        assert len(result.output) <= 50 + 100  # some overhead for truncation message

    @patch("subprocess.run")
    def test_execute_combines_stdout_stderr(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="out\n", stderr="err\n"
        )

        result = sandbox.execute("failing_cmd")

        assert "out" in result.output
        assert "err" in result.output
        assert result.exit_code == 1

    @patch("subprocess.run")
    def test_execute_without_start_raises(self, mock_run, sandbox):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.execute("echo hello")


class TestLifecycleHooks:
    @patch("subprocess.run")
    def test_hooks_run_before_ready(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="abc123container\n",
            stderr="",
        )
        invocations: list[object] = []

        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        s.start()
        assert invocations == [s]

    @patch("subprocess.run")
    def test_hooks_re_run_on_restart(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """A second start, such as device reselection, reruns the hooks."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="abc123container\n",
            stderr="",
        )
        invocations: list[object] = []

        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            lifecycle_hooks=[_RecordingHooks(invocations)],
        )
        s.start()
        s.start()
        assert invocations == [s, s]

    @patch("subprocess.run")
    def test_setup_failure_preserves_error_when_stop_fails(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        def run(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
            if cmd[:2] == ["docker", "run"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="abc123\n", stderr=""
                )
            if cmd[:2] == ["docker", "stop"]:
                raise OSError("Docker daemon disconnected")  # noqa: TRY003  # tracked: #288
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = run
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            lifecycle_hooks=[_FailingHooks()],
        )

        try:
            with pytest.raises(SandboxLifecycleError, match="_FailingHooks failed") as error:
                sandbox.start()

            assert isinstance(error.value.__cause__, ValueError)
            assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
            assert "abc123" not in _live_containers
            commands = [call.args[0] for call in mock_run.call_args_list]
            assert ["docker", "stop", "abc123"] in commands
            assert ["docker", "rm", "-f", "abc123"] in commands
        finally:
            _live_containers.pop("abc123", None)

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    @patch("subprocess.run")
    def test_setup_failure_retains_container_for_retry_when_removal_fails(  # noqa: ANN201  # tracked: #288
        self,
        mock_run,  # noqa: ANN001  # tracked: #288
        cleanup_mode,  # noqa: ANN001  # tracked: #288
        tmp_path,  # noqa: ANN001  # tracked: #288
    ):
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        def run(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
            if cmd[:2] == ["docker", "run"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="abc123\n", stderr=""
                )
            if cmd[:2] in (["docker", "stop"], ["docker", "rm"]):
                if cleanup_mode == "raises":
                    raise OSError("Docker daemon disconnected")  # noqa: TRY003  # tracked: #288
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="daemon unavailable"
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = run
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="test-image",
            lifecycle_hooks=[_FailingHooks()],
        )

        try:
            with pytest.raises(SandboxLifecycleError, match="_FailingHooks failed"):
                sandbox.start()

            assert sandbox._container_id == "abc123"  # noqa: SLF001  # tracked: #288
            assert "abc123" in _live_containers
        finally:
            _live_containers.pop("abc123", None)
            sandbox._container_id = None  # noqa: SLF001  # tracked: #288


class TestStop:
    @patch("subprocess.run")
    def test_stop_removes_container(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        sandbox.stop()

        # Should call docker stop then docker rm
        assert mock_run.call_count == 2
        stop_cmd = mock_run.call_args_list[0][0][0]
        rm_cmd = mock_run.call_args_list[1][0][0]
        assert stop_cmd[0] == "docker" and "stop" in stop_cmd  # noqa: PT018  # tracked: #288
        assert rm_cmd[0] == "docker" and "rm" in rm_cmd  # noqa: PT018  # tracked: #288

    @patch("subprocess.run")
    def test_stop_idempotent(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        sandbox.stop()
        mock_run.reset_mock()

        # Second stop should be a no-op
        sandbox.stop()
        assert mock_run.call_count == 0

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    @patch("subprocess.run")
    def test_failed_removal_retains_ownership_and_can_be_retried(  # noqa: ANN201  # tracked: #288
        self,
        mock_run,  # noqa: ANN001  # tracked: #288
        cleanup_mode,  # noqa: ANN001  # tracked: #288
        sandbox,  # noqa: ANN001  # tracked: #288
    ):
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        sandbox._container_id = "abc123"  # noqa: SLF001  # tracked: #288
        _live_containers["abc123"] = "vibesys-test"

        def fail_removal(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
            if cmd[1] == "rm":
                if cleanup_mode == "raises":
                    raise OSError("Docker daemon disconnected")  # noqa: TRY003  # tracked: #288
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="daemon unavailable"
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = fail_removal
        try:
            expected_error = OSError if cleanup_mode == "raises" else RuntimeError
            with pytest.raises(expected_error):
                sandbox.stop()

            assert sandbox._container_id == "abc123"  # noqa: SLF001  # tracked: #288
            assert "abc123" in _live_containers

            mock_run.side_effect = None
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            sandbox.stop()

            assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
            assert "abc123" not in _live_containers
        finally:
            _live_containers.pop("abc123", None)
            sandbox._container_id = None  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_stop_failure_does_not_prevent_forced_removal(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        sandbox._container_id = "abc123"  # noqa: SLF001  # tracked: #288
        _live_containers["abc123"] = "vibesys-test"
        mock_run.side_effect = [
            OSError("Docker daemon disconnected"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        sandbox.stop()

        assert mock_run.call_args_list[1].args[0] == ["docker", "rm", "-f", "abc123"]
        assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
        assert "abc123" not in _live_containers

    @patch("subprocess.run")
    def test_already_absent_container_clears_ownership(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        sandbox._container_id = "abc123"  # noqa: SLF001  # tracked: #288
        _live_containers["abc123"] = "vibesys-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: No such container: abc123"
        )

        sandbox.stop()

        assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
        assert "abc123" not in _live_containers

    @patch("subprocess.run")
    def test_removal_already_in_progress_clears_ownership(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        sandbox._container_id = "abc123"  # noqa: SLF001  # tracked: #288
        _live_containers["abc123"] = "vibesys-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Error response from daemon: removal of container abc123 is already in progress"
            ),
        )

        sandbox.stop()

        assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288
        assert "abc123" not in _live_containers

    @patch("subprocess.run")
    def test_keyboard_interrupt_is_not_swallowed(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        sandbox._container_id = "abc123"  # noqa: SLF001  # tracked: #288
        _live_containers["abc123"] = "vibesys-test"
        mock_run.side_effect = KeyboardInterrupt()

        try:
            with pytest.raises(KeyboardInterrupt):
                sandbox.stop()

            assert mock_run.call_count == 1
            assert sandbox._container_id == "abc123"  # noqa: SLF001  # tracked: #288
            assert "abc123" in _live_containers
        finally:
            _live_containers.pop("abc123", None)
            sandbox._container_id = None  # noqa: SLF001  # tracked: #288


class TestIdProperty:
    @patch("subprocess.run")
    def test_id_property(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123def456ghi789\n", stderr=""
        )
        sandbox.start()

        assert sandbox.id.startswith("vibesys-")
        assert len(sandbox.id) > len("vibesys-")


class TestContainerIdProperty:
    @patch("subprocess.run")
    def test_container_id_returns_running_container(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123def456ghi789\n", stderr=""
        )
        sandbox.start()

        assert sandbox.container_id == "abc123def456ghi789"

    def test_container_id_before_start_raises(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        with pytest.raises(RuntimeError, match="no running container"):
            _ = sandbox.container_id

    @patch("subprocess.run")
    def test_container_id_after_stop_raises(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123def456ghi789\n", stderr=""
        )
        sandbox.start()
        sandbox.stop()

        with pytest.raises(RuntimeError, match="no running container"):
            _ = sandbox.container_id


class TestUploadFiles:
    @patch("subprocess.run")
    def test_upload_files(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        results = sandbox.upload_files([("test.py", b"print('hello')")])

        assert len(results) == 1
        assert results[0].path == "test.py"
        assert results[0].error is None
        # Should have called docker cp
        assert any("cp" in " ".join(c[0][0]) for c in mock_run.call_args_list)


class TestDownloadFiles:
    @patch("subprocess.run")
    def test_download_files(self, mock_run, sandbox, tmp_path):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        # Mock docker cp to create the file
        def mock_docker_cp(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001  # tracked: #288
            # Simulate docker cp by creating the file in the temp dir
            if "cp" in cmd:
                # Extract dest path from command
                dest = cmd[-1]
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"file content")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_docker_cp

        results = sandbox.download_files(["test.py"])

        assert len(results) == 1
        assert results[0].path == "test.py"
        assert results[0].content == b"file content"


class TestContextManager:
    @patch("subprocess.run")
    def test_context_manager(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        with sandbox:
            assert sandbox._container_id is not None  # noqa: SLF001  # tracked: #288

        # After exit, container should be stopped
        assert sandbox._container_id is None  # noqa: SLF001  # tracked: #288


class TestPathTranslation:
    def test_absolute_virtual_path_gets_workspace_prefix(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        assert sandbox._vpath("/reference/model") == "/workspace/reference/model"  # noqa: SLF001  # tracked: #288

    def test_root_path_maps_to_workspace(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        assert sandbox._vpath("/") == "/workspace/"  # noqa: SLF001  # tracked: #288

    def test_already_workspace_path_unchanged(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        assert sandbox._vpath("/workspace/foo") == "/workspace/foo"  # noqa: SLF001  # tracked: #288

    def test_workspace_root_unchanged(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        assert sandbox._vpath("/workspace") == "/workspace"  # noqa: SLF001  # tracked: #288

    def test_relative_path_unchanged(self, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        assert sandbox._vpath("reference/model") == "reference/model"  # noqa: SLF001  # tracked: #288

    def test_passthrough_path_not_rewritten(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Paths in passthrough_paths should not get /workspace prepended."""
        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            passthrough_paths=["/model"],
        )
        assert s._vpath("/model") == "/model"  # noqa: SLF001  # tracked: #288
        assert s._vpath("/model/config.json") == "/model/config.json"  # noqa: SLF001  # tracked: #288
        # Other absolute paths should still be rewritten
        assert s._vpath("/other") == "/workspace/other"  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_read_translates_path(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        """read() should translate the path before delegating to BaseSandbox."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        with patch.object(
            type(sandbox).__bases__[0], "read", return_value="content"
        ) as mock_super_read:
            sandbox.read("/reference/reference.py")
            mock_super_read.assert_called_once_with("/workspace/reference/reference.py", 0, 2000)

    @patch("subprocess.run")
    def test_ls_translates_path(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        """ls_info('/') should translate to /workspace/."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()

        with patch.object(type(sandbox).__bases__[0], "ls_info", return_value=[]) as mock_super_ls:
            sandbox.ls_info("/")
            mock_super_ls.assert_called_once_with("/workspace/")


class TestCleanupOnExit:
    @pytest.fixture(autouse=True)
    def _clear_live_containers(self):  # noqa: ANN202  # tracked: #288
        """Isolate the global _live_containers registry between tests."""
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        _live_containers.clear()
        yield
        _live_containers.clear()

    @patch("subprocess.run")
    def test_live_containers_tracked(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import _live_containers  # noqa: PLC0415  # tracked: #288

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()

        assert sandbox._container_id in _live_containers  # noqa: SLF001  # tracked: #288

        sandbox.stop()
        assert "abc123" not in _live_containers

    @patch("subprocess.run")
    def test_cleanup_containers_stops_all(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import (  # noqa: PLC0415  # tracked: #288
            _cleanup_containers,
            _live_containers,
        )

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="container_xyz\n", stderr=""
        )
        sandbox.start()
        assert "container_xyz" in _live_containers

        mock_run.reset_mock()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        _cleanup_containers()

        assert len(_live_containers) == 0
        # Should have called docker stop + docker rm
        stop_calls = [c for c in mock_run.call_args_list if "stop" in c[0][0]]
        rm_calls = [c for c in mock_run.call_args_list if "rm" in c[0][0]]
        assert len(stop_calls) == 1
        assert len(rm_calls) == 1

    @pytest.mark.parametrize("cleanup_mode", ["raises", "nonzero"])
    @patch("subprocess.run")
    def test_cleanup_retains_failed_removal_for_retry(self, mock_run, cleanup_mode):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import (  # noqa: PLC0415  # tracked: #288
            _cleanup_containers,
            _live_containers,
        )

        _live_containers["abc123"] = "vibesys-test"

        def fail_removal(cmd, **_kwargs):  # noqa: ANN001, ANN003, ANN202  # tracked: #288
            if cmd[1] == "rm":
                if cleanup_mode == "raises":
                    raise OSError("Docker daemon disconnected")  # noqa: TRY003  # tracked: #288
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="daemon unavailable"
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = fail_removal

        _cleanup_containers()

        assert "abc123" in _live_containers
        rm_call = mock_run.call_args_list[1]
        assert rm_call.args[0] == ["docker", "rm", "-f", "abc123"]
        assert rm_call.kwargs["timeout"] == 10

    @patch("subprocess.run")
    def test_cleanup_forces_removal_after_stop_exception(self, mock_run):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox.docker_sandbox import (  # noqa: PLC0415  # tracked: #288
            _cleanup_containers,
            _live_containers,
        )

        _live_containers["abc123"] = "vibesys-test"
        mock_run.side_effect = [
            OSError("Docker daemon disconnected"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        _cleanup_containers()

        assert mock_run.call_args_list[1].args[0] == ["docker", "rm", "-f", "abc123"]
        assert "abc123" not in _live_containers

    def test_sigint_defers_container_cleanup_until_stack_unwinds(self, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        from vs_sandbox import docker_sandbox  # noqa: PLC0415  # tracked: #288

        cleanup_calls: list[bool] = []
        original_calls: list[tuple[int, object]] = []

        def original_handler(signum, frame):  # noqa: ANN001, ANN202  # tracked: #288
            original_calls.append((signum, frame))
            raise KeyboardInterrupt

        monkeypatch.setattr(
            docker_sandbox,
            "_cleanup_containers",
            lambda: cleanup_calls.append(True),
        )
        monkeypatch.setattr(docker_sandbox, "_original_sigint", original_handler)

        with patch.object(docker_sandbox.signal, "signal") as restore_handler:  # noqa: SIM117  # tracked: #288
            with pytest.raises(KeyboardInterrupt):
                docker_sandbox._sigint_handler(2, None)  # noqa: SLF001  # tracked: #288

        restore_handler.assert_called_once_with(
            docker_sandbox.signal.SIGINT,
            original_handler,
        )
        assert original_calls == [(2, None)]
        assert cleanup_calls == []


class TestWrite:
    @patch("subprocess.run")
    def test_write_uses_docker_cp(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        """write() should use docker cp instead of shelling out content."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        result = sandbox.write("/test.py", "print('hello')")

        assert result.error is None
        # Should have called mkdir -p then docker cp
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert any("mkdir" in cmd for cmd in cmds)
        assert any("cp" in cmd for cmd in cmds)
        # Should NOT have used docker exec bash -c (which would inline content)
        exec_bash_calls = [c for c in cmds if "exec" in c and "bash" in c and "-c" in c]
        assert len(exec_bash_calls) == 0

    @patch("subprocess.run")
    def test_write_large_content(self, mock_run, sandbox):  # noqa: ANN001, ANN201  # tracked: #288
        """write() should handle content larger than shell arg limit."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        large_content = "x" * 500_000
        result = sandbox.write("/big_file.txt", large_content)

        assert result.error is None

    @patch("subprocess.run")
    def test_write_without_start_raises(self, mock_run, sandbox):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.write("/test.py", "content")


class TestEnvVars:
    @patch("subprocess.run")
    def test_env_vars_passed_to_docker_run(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="pytorch:latest",
            env={"MY_KEY": "my_value", "OTHER": "thing"},
        )

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        cmd_str = " ".join(cmd)
        assert "-e" in cmd
        assert "MY_KEY=my_value" in cmd_str
        assert "OTHER=thing" in cmd_str

    @patch("subprocess.run")
    def test_credential_values_reach_the_container_but_not_the_log(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        log_path = tmp_path / "docker.log"
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image="pytorch:latest",
            env={
                "ANTHROPIC_AUTH_TOKEN": "sk-secret-token",
                "ANTHROPIC_BASE_URL": "https://proxy.invalid/v1",
                "PYTHONPATH": "/opt/vibesys",
            },
            log_path=log_path,
        )

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd_str = " ".join(mock_run.call_args_list[0][0][0])
        assert "ANTHROPIC_AUTH_TOKEN=sk-secret-token" in cmd_str

        log_text = log_path.read_text()
        assert "sk-secret-token" not in log_text
        assert "ANTHROPIC_AUTH_TOKEN=<redacted>" in log_text
        # Endpoint selection is a diagnostic, not a credential.
        assert "ANTHROPIC_BASE_URL=https://proxy.invalid/v1" in log_text
        assert "PYTHONPATH=/opt/vibesys" in log_text

    @patch("subprocess.run")
    def test_credential_values_are_omitted_from_workspace_metadata(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image="pytorch:latest",
            env={"ANTHROPIC_API_KEY": "sk-secret-key", "PYTHONPATH": "/opt/vibesys"},
        )

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        metadata_text = (workspace / ".docker_metadata.json").read_text()
        assert "sk-secret-key" not in metadata_text
        assert json.loads(metadata_text)["env"] == {"PYTHONPATH": "/opt/vibesys"}

    @patch("subprocess.run")
    def test_start_failure_error_does_not_expose_credentials(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="pytorch:latest",
            env={"ANTHROPIC_AUTH_TOKEN": "sk-secret-token"},
        )

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=125, stdout="", stderr="boom"
        )

        with pytest.raises(RuntimeError) as excinfo:
            sandbox.start()

        assert "sk-secret-token" not in str(excinfo.value)
        assert "ANTHROPIC_AUTH_TOKEN=<redacted>" in str(excinfo.value)


class TestDevicePassthrough:
    @patch("subprocess.run")
    def test_devices_emit_device_flags_and_no_gpus(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="public.ecr.aws/neuron/pytorch-inference-neuronx:latest",
            gpus=None,  # Neuron uses --device, not --gpus
            devices=["/dev/neuron0", "/dev/neuron1"],
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        assert "--gpus" not in cmd
        # Each device forwarded with its own --device flag.
        assert cmd.count("--device") == 2
        for dev in ("/dev/neuron0", "/dev/neuron1"):
            i = cmd.index(dev)
            assert cmd[i - 1] == "--device"
        assert sandbox._metadata["devices"] == ["/dev/neuron0", "/dev/neuron1"]  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_group_add_emits_group_add_flags(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """AMD /dev/kfd and /dev/dri/* are group-owned; without --group-add the
        container user cannot open them and every HIP call fails at runtime."""
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="rocm/pytorch:latest",
            gpus=None,
            devices=["/dev/kfd", "/dev/dri/renderD128"],
            group_add=["video", "render"],
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        assert cmd.count("--group-add") == 2
        for group in ("video", "render"):
            i = cmd.index(group)
            assert cmd[i - 1] == "--group-add"
        # Recorded so a reattaching shell reconstructs the same device access;
        # without it the container user cannot open /dev/kfd and every HIP call
        # fails — the exact failure --group-add exists to prevent.
        assert sandbox._metadata["group_add"] == ["video", "render"]  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_no_group_add_by_default(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        cmd = mock_run.call_args_list[0][0][0]
        assert "--group-add" not in cmd

    @patch("subprocess.run")
    def test_no_devices_by_default(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        cmd = mock_run.call_args_list[0][0][0]
        assert "--device" not in cmd


class TestEntrypointOverride:
    @patch("subprocess.run")
    def test_entrypoint_override_emitted_before_image(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="public.ecr.aws/neuron/pytorch-inference-neuronx:latest",
            gpus=None,
            entrypoint="",  # clear the DLC's baked-in model-server entrypoint
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        cmd = mock_run.call_args_list[0][0][0]
        assert "--entrypoint" in cmd
        ep_idx = cmd.index("--entrypoint")
        assert cmd[ep_idx + 1] == ""
        # Override must precede the image positional, which precedes the command.
        img_idx = cmd.index(sandbox._image)  # noqa: SLF001  # tracked: #288
        assert ep_idx < img_idx
        assert cmd[-2:] == ["sleep", "infinity"]
        assert sandbox._metadata["entrypoint"] == ""  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_no_entrypoint_flag_by_default(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        cmd = mock_run.call_args_list[0][0][0]
        assert "--entrypoint" not in cmd


class TestShmSize:
    @patch("subprocess.run")
    def test_shm_size_emitted(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="img",
            shm_size="16g",
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        cmd = mock_run.call_args_list[0][0][0]
        assert "--shm-size" in cmd
        assert cmd[cmd.index("--shm-size") + 1] == "16g"
        assert sandbox._metadata["shm_size"] == "16g"  # noqa: SLF001  # tracked: #288

    @patch("subprocess.run")
    def test_no_shm_size_by_default(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc\n", stderr=""
        )
        sandbox.start()
        assert "--shm-size" not in mock_run.call_args_list[0][0][0]


class TestAutoRemove:
    @patch("subprocess.run")
    def test_auto_remove_emits_rm_flag(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"), image="img", auto_remove=True
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc\n", stderr=""
        )
        sandbox.start()
        assert "--rm" in mock_run.call_args_list[0][0][0]

    @patch("subprocess.run")
    def test_no_rm_by_default(self, mock_run, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        sandbox = DockerSandbox(host_workspace=str(tmp_path / "workspace"), image="img")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc\n", stderr=""
        )
        sandbox.start()
        assert "--rm" not in mock_run.call_args_list[0][0][0]


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
