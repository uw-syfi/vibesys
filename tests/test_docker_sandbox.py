"""Tests for DockerSandbox — all mock subprocess.run, no Docker required."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from vibeserve_agent.sandbox.docker_sandbox import DockerSandbox


@pytest.fixture()
def sandbox(tmp_path):
    return DockerSandbox(
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
    )


@pytest.fixture()
def sandbox_with_mounts(tmp_path):
    return DockerSandbox(
        host_workspace=str(tmp_path / "workspace"),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpus="all",
        bind_mounts=[
            (str(tmp_path / "model_weights"), "/workspace/reference/model", True),
            (str(tmp_path / "acc_checker"), "/workspace/acc_checker", True),
        ],
    )


class TestStart:
    @patch.dict("os.environ", {}, clear=False)
    @patch("subprocess.run")
    def test_start_runs_docker_run_with_correct_args(self, mock_run, sandbox):
        # Remove CUDA_VISIBLE_DEVICES so fallback to "all" is tested
        import os
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
        # Container should be named with vibeserve prefix
        assert "--name" in cmd
        name_idx = cmd.index("--name")
        assert cmd[name_idx + 1].startswith("vibeserve-")
        assert docker_run_call.kwargs["timeout"] == 120

    @patch("subprocess.run")
    def test_start_docker_run_timeout_raises_clear_error(self, mock_run, sandbox):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["docker", "run"], timeout=120,
        )

        with pytest.raises(RuntimeError, match="Timed out starting Docker container"):
            sandbox.start()

    @patch("subprocess.run")
    def test_start_failure_removes_created_container(self, mock_run, sandbox):
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=125, stdout="abc123container\n", stderr="gpu error",
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        with pytest.raises(RuntimeError, match="Failed to start Docker container"):
            sandbox.start()

        rm_cmd = mock_run.call_args_list[1][0][0]
        assert rm_cmd == ["docker", "rm", "abc123container"]

    @patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "3,5,7"})
    @patch("subprocess.run")
    def test_start_uses_first_cuda_visible_device(self, mock_run, sandbox):
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
    def test_start_bind_mounts_workspace(self, mock_run, sandbox):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        cmd = mock_run.call_args_list[0][0][0]
        # Should have -v for workspace mount
        cmd_str = " ".join(cmd)
        assert f"{sandbox._host_workspace}:/workspace" in cmd_str

    @patch("subprocess.run")
    def test_start_bind_mounts_extra(self, mock_run, sandbox_with_mounts):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox_with_mounts.start()

        cmd = mock_run.call_args_list[0][0][0]
        cmd_str = " ".join(cmd)
        # Extra bind mounts should be read-only
        assert "/workspace/reference/model:ro" in cmd_str
        assert "/workspace/acc_checker:ro" in cmd_str

    @patch("subprocess.run")
    def test_start_installs_uv(self, mock_run, sandbox):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        sandbox.start()

        # Second call should install uv
        assert len(mock_run.call_args_list) == 2
        uv_call = mock_run.call_args_list[1]
        cmd = uv_call[0][0]
        assert "docker" in cmd[0]
        assert "exec" in cmd
        cmd_str = " ".join(cmd)
        assert "pip install uv" in cmd_str


class TestExecute:
    @patch("subprocess.run")
    def test_execute_runs_docker_exec(self, mock_run, sandbox):
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
    def test_execute_timeout(self, mock_run, sandbox):
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
    def test_execute_output_truncation(self, mock_run, sandbox):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        # Set small max_output_bytes
        sandbox._max_output_bytes = 50
        big_output = "x" * 200
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=big_output, stderr=""
        )

        result = sandbox.execute("cat bigfile")

        assert result.truncated is True
        assert len(result.output) <= 50 + 100  # some overhead for truncation message

    @patch("subprocess.run")
    def test_execute_combines_stdout_stderr(self, mock_run, sandbox):
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
    def test_execute_without_start_raises(self, mock_run, sandbox):
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.execute("echo hello")


class TestSetupFns:
    @patch("subprocess.run")
    def test_setup_fns_run_after_start(self, mock_run, tmp_path):
        """setup_fns receive the sandbox and run at the end of start()."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr="",
        )
        invocations: list[DockerSandbox] = []

        def fn(sb: DockerSandbox) -> None:
            invocations.append(sb)

        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            setup_fns=[fn],
        )
        s.start()
        assert invocations == [s]

    @patch("subprocess.run")
    def test_setup_fns_re_run_on_restart(self, mock_run, tmp_path):
        """A second start() (e.g. after stop() from reselect_device) re-runs."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123container\n", stderr="",
        )
        calls = 0

        def fn(_sb: DockerSandbox) -> None:
            nonlocal calls
            calls += 1

        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            setup_fns=[fn],
        )
        s.start()
        s.start()
        assert calls == 2


class TestStop:
    @patch("subprocess.run")
    def test_stop_removes_container(self, mock_run, sandbox):
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
        assert stop_cmd[0] == "docker" and "stop" in stop_cmd
        assert rm_cmd[0] == "docker" and "rm" in rm_cmd

    @patch("subprocess.run")
    def test_stop_idempotent(self, mock_run, sandbox):
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


class TestIdProperty:
    @patch("subprocess.run")
    def test_id_property(self, mock_run, sandbox):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123def456ghi789\n", stderr=""
        )
        sandbox.start()

        assert sandbox.id.startswith("vibeserve-")
        assert len(sandbox.id) > len("vibeserve-")


class TestUploadFiles:
    @patch("subprocess.run")
    def test_upload_files(self, mock_run, sandbox):
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
    def test_download_files(self, mock_run, sandbox, tmp_path):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()
        mock_run.reset_mock()

        # Mock docker cp to create the file
        def mock_docker_cp(cmd, **kwargs):
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
    def test_context_manager(self, mock_run, sandbox):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )

        with sandbox:
            assert sandbox._container_id is not None

        # After exit, container should be stopped
        assert sandbox._container_id is None


class TestPathTranslation:
    def test_absolute_virtual_path_gets_workspace_prefix(self, sandbox):
        assert sandbox._vpath("/reference/model") == "/workspace/reference/model"

    def test_root_path_maps_to_workspace(self, sandbox):
        assert sandbox._vpath("/") == "/workspace/"

    def test_already_workspace_path_unchanged(self, sandbox):
        assert sandbox._vpath("/workspace/foo") == "/workspace/foo"

    def test_workspace_root_unchanged(self, sandbox):
        assert sandbox._vpath("/workspace") == "/workspace"

    def test_relative_path_unchanged(self, sandbox):
        assert sandbox._vpath("reference/model") == "reference/model"

    def test_passthrough_path_not_rewritten(self, tmp_path):
        """Paths in passthrough_paths should not get /workspace prepended."""
        s = DockerSandbox(
            host_workspace=str(tmp_path / "workspace"),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            passthrough_paths=["/model"],
        )
        assert s._vpath("/model") == "/model"
        assert s._vpath("/model/config.json") == "/model/config.json"
        # Other absolute paths should still be rewritten
        assert s._vpath("/other") == "/workspace/other"

    @patch("subprocess.run")
    def test_read_translates_path(self, mock_run, sandbox):
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
            mock_super_read.assert_called_once_with(
                "/workspace/reference/reference.py", 0, 2000
            )

    @patch("subprocess.run")
    def test_ls_translates_path(self, mock_run, sandbox):
        """ls_info('/') should translate to /workspace/."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()

        with patch.object(
            type(sandbox).__bases__[0], "ls_info", return_value=[]
        ) as mock_super_ls:
            sandbox.ls_info("/")
            mock_super_ls.assert_called_once_with("/workspace/")


class TestCleanupOnExit:
    @pytest.fixture(autouse=True)
    def _clear_live_containers(self):
        """Isolate the global _live_containers registry between tests."""
        from vibeserve_agent.sandbox.docker_sandbox import _live_containers
        _live_containers.clear()
        yield
        _live_containers.clear()

    @patch("subprocess.run")
    def test_live_containers_tracked(self, mock_run, sandbox):
        from vibeserve_agent.sandbox.docker_sandbox import _live_containers

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abc123\n", stderr=""
        )
        sandbox.start()

        assert sandbox._container_id in _live_containers

        sandbox.stop()
        assert "abc123" not in _live_containers

    @patch("subprocess.run")
    def test_cleanup_containers_stops_all(self, mock_run, sandbox):
        from vibeserve_agent.sandbox.docker_sandbox import _cleanup_containers, _live_containers

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


class TestWrite:
    @patch("subprocess.run")
    def test_write_uses_docker_cp(self, mock_run, sandbox):
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
        exec_bash_calls = [
            c for c in cmds
            if "exec" in c and "bash" in c and "-c" in c
        ]
        assert len(exec_bash_calls) == 0

    @patch("subprocess.run")
    def test_write_large_content(self, mock_run, sandbox):
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
    def test_write_without_start_raises(self, mock_run, sandbox):
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.write("/test.py", "content")


class TestEnvVars:
    @patch("subprocess.run")
    def test_env_vars_passed_to_docker_run(self, mock_run, tmp_path):
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
