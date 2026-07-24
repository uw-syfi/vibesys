"""Tests for ModalSandbox — all mock modal.Sandbox/Volume, no Modal auth required."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def mock_modal(monkeypatch):
    """Patch modal.Sandbox.create, Image.from_registry, App.lookup, Volume.from_name/delete."""
    import modal

    from vs_sandbox.modal_sandbox import _live_sandboxes

    _live_sandboxes.clear()

    fake_sandbox = MagicMock()
    fake_sandbox.object_id = "sb-abc123"
    # exec returns a process with stdout/stderr .read() and .wait()
    fake_proc = MagicMock()
    fake_proc.stdout.read.return_value = "hello\n"
    fake_proc.stderr.read.return_value = ""
    fake_proc.wait.return_value = 0
    fake_sandbox.exec.return_value = fake_proc
    # filesystem namespace with read_bytes/write_bytes/write_text/make_directory
    fake_fs = MagicMock()
    fake_fs.read_bytes.return_value = b"file-contents"
    fake_sandbox.filesystem = fake_fs

    fake_volume = MagicMock()
    fake_upload_cm = MagicMock()
    fake_upload_cm.__enter__ = MagicMock(return_value=fake_upload_cm)
    fake_upload_cm.__exit__ = MagicMock(return_value=False)
    fake_volume.batch_upload.return_value = fake_upload_cm
    fake_volume.listdir.return_value = []

    fake_objects = MagicMock()
    monkeypatch.setattr(modal.App, "lookup", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(modal.Image, "from_registry", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(modal.Sandbox, "create", MagicMock(return_value=fake_sandbox))
    monkeypatch.setattr(modal.Volume, "from_name", MagicMock(return_value=fake_volume))
    monkeypatch.setattr(modal.Volume, "objects", fake_objects)
    yield {
        "sandbox": fake_sandbox,
        "proc": fake_proc,
        "volume": fake_volume,
        "objects": fake_objects,
    }

    leaked = list(_live_sandboxes)
    _live_sandboxes.clear()
    assert not leaked, f"ModalSandbox tests leaked live sandboxes: {leaked}"


@pytest.fixture()
def sandbox(tmp_path, mock_modal):
    from vs_sandbox.modal_sandbox import ModalSandbox

    ws = tmp_path / "workspace"
    ws.mkdir()
    sb = ModalSandbox(
        host_workspace=str(ws),
        image="nvcr.io/nvidia/pytorch:25.04-py3",
        gpu="H100",
    )
    try:
        yield sb
    finally:
        sb.stop()


class TestVpath:
    def test_rewrites_virtual_root_paths(self, sandbox):
        assert sandbox._vpath("/foo/bar.py") == "/workspace/foo/bar.py"

    def test_preserves_container_root_paths(self, sandbox):
        assert sandbox._vpath("/workspace/foo") == "/workspace/foo"

    def test_preserves_passthrough(self, tmp_path, mock_modal):
        from vs_sandbox.modal_sandbox import ModalSandbox

        sb = ModalSandbox(
            host_workspace=str(tmp_path),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            passthrough_paths=["/model"],
        )
        assert sb._vpath("/model/weights.bin") == "/model/weights.bin"
        assert sb._vpath("/foo") == "/workspace/foo"

    def test_relative_paths_untouched(self, sandbox):
        assert sandbox._vpath("foo/bar.py") == "foo/bar.py"


class TestStart:
    def test_start_creates_sandbox_with_gpu_and_timeout(self, sandbox, mock_modal):
        import modal

        sandbox.start()
        modal.Sandbox.create.assert_called_once()
        kwargs = modal.Sandbox.create.call_args.kwargs
        assert kwargs["gpu"] == "H100"
        assert kwargs["workdir"] == "/workspace"
        assert "/workspace" in kwargs["volumes"]

    def test_start_mounts_model_volume_when_name_given(self, tmp_path, mock_modal):
        import modal

        from vs_sandbox.modal_sandbox import ModalSandbox

        with ModalSandbox(
            host_workspace=str(tmp_path),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            model_volume_name="vibesys-models",
        ):
            kwargs = modal.Sandbox.create.call_args.kwargs
            assert "/model" in kwargs["volumes"]

    def test_start_uploads_bind_mounts_into_workspace_volume(
        self,
        tmp_path,
        mock_modal,
    ):
        from vs_sandbox.modal_sandbox import ModalSandbox

        ws = tmp_path / "ws"
        ws.mkdir()
        bench = tmp_path / "bench"
        bench.mkdir()
        (bench / "a.py").write_text("x")
        with ModalSandbox(
            host_workspace=str(ws),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            bind_mounts=[(str(bench), "/workspace/bench", True)],
        ):
            upload_cm = mock_modal["volume"].batch_upload.return_value
            calls = upload_cm.put_directory.call_args_list
            assert any("bench" in str(c) and "'/bench'" in str(c) for c in calls)

    def test_start_skips_model_bind_mount_from_workspace_volume(
        self,
        tmp_path,
        mock_modal,
    ):
        from vs_sandbox.modal_sandbox import ModalSandbox

        ws = tmp_path / "ws"
        ws.mkdir()
        with ModalSandbox(
            host_workspace=str(ws),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            bind_mounts=[("/host/model", "/model", True)],
        ):
            upload_cm = mock_modal["volume"].batch_upload.return_value
            for c in upload_cm.put_directory.call_args_list:
                assert "/host/model" not in str(c)

    def test_start_skips_hf_cache_bind_mount_from_workspace_volume(
        self,
        tmp_path,
        mock_modal,
    ):
        from vs_sandbox.modal_sandbox import ModalSandbox

        ws = tmp_path / "ws"
        ws.mkdir()
        cache = tmp_path / ".hf_cache"
        cache.mkdir()
        model_cache = cache / "models--example"
        model_cache.mkdir()
        (model_cache / "weights.bin").write_text("large")
        with ModalSandbox(
            host_workspace=str(ws),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            bind_mounts=[(str(model_cache), "/workspace/.cache/models--example", True)],
        ):
            upload_cm = mock_modal["volume"].batch_upload.return_value
            for c in upload_cm.put_directory.call_args_list:
                assert ".hf_cache" not in str(c)

    def test_start_excludes_local_runtime_dirs_from_workspace_upload(
        self,
        tmp_path,
        mock_modal,
    ):
        from vs_sandbox.modal_sandbox import ModalSandbox

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "server.py").write_text("print('ok')")
        for dirname, filename in (
            ("exp_env", "run.log"),
            (".venv", "python"),
            (".git", "config"),
        ):
            d = ws / dirname
            d.mkdir()
            (d / filename).write_text("local-only")

        with ModalSandbox(
            host_workspace=str(ws),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
        ):
            upload_cm = mock_modal["volume"].batch_upload.return_value
            uploaded = [str(c) for c in upload_cm.put_file.call_args_list]
            assert any("server.py" in c for c in uploaded)
            assert not any("exp_env" in c for c in uploaded)
            assert not any(".venv" in c for c in uploaded)
            assert not any(".git" in c for c in uploaded)

    def test_start_uploads_minimal_codex_auth_snapshot(
        self,
        tmp_path,
        mock_modal,
    ):
        from vs_sandbox.modal_sandbox import ModalSandbox

        ws = tmp_path / "ws"
        ws.mkdir()
        codex = tmp_path / ".codex"
        codex.mkdir()
        for name in ("auth.json", "config.toml", "installation_id", "version.json"):
            (codex / name).write_text("x")
        (codex / "state_5.sqlite").write_text("state")
        (codex / "state_5.sqlite-wal").write_text("state")
        (codex / "sessions").mkdir()
        (codex / "sessions" / "large.jsonl").write_text("skip")

        sb = ModalSandbox(
            host_workspace=str(ws),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
        )
        snapshot = tmp_path / "snapshot"
        sb._copy_bind_mount_snapshot(codex, snapshot)

        assert (snapshot / "auth.json").exists()
        assert (snapshot / "config.toml").exists()
        assert (snapshot / "state_5.sqlite").exists()
        assert (snapshot / "state_5.sqlite-wal").exists()
        assert not (snapshot / "sessions").exists()


class TestExecute:
    def test_execute_runs_bash_command_in_workspace(self, sandbox, mock_modal):
        sandbox.start()
        resp = sandbox.execute("ls")
        assert resp.exit_code == 0
        assert resp.output == "hello\n"
        mock_modal["sandbox"].exec.assert_called_with(
            "bash",
            "-c",
            "ls",
            workdir="/workspace",
            timeout=300,
        )

    def test_execute_respects_custom_timeout(self, sandbox, mock_modal):
        sandbox.start()
        sandbox.execute("sleep 1", timeout=42)
        # Last exec call should have timeout=42
        call = mock_modal["sandbox"].exec.call_args_list[-1]
        assert call.kwargs["timeout"] == 42

    def test_execute_truncates_long_output(self, sandbox, mock_modal):
        sandbox.start()
        mock_modal["proc"].stdout.read.return_value = "a" * 200_000
        mock_modal["proc"].stderr.read.return_value = ""
        sandbox._max_output_bytes = 1000
        resp = sandbox.execute("big")
        assert resp.truncated
        assert "truncated" in resp.output

    def test_execute_handles_exception_gracefully(self, sandbox, mock_modal):
        sandbox.start()
        mock_modal["sandbox"].exec.side_effect = RuntimeError("boom")
        resp = sandbox.execute("x")
        assert resp.exit_code == -1
        assert "boom" in resp.output

    def test_execute_raises_when_not_started(self, sandbox):
        with pytest.raises(RuntimeError, match="not started"):
            sandbox.execute("ls")


class TestStop:
    def test_stop_terminates_and_deletes_volume(self, sandbox, mock_modal):
        sandbox.start()
        sandbox.stop()
        mock_modal["sandbox"].terminate.assert_called_once()
        mock_modal["objects"].delete.assert_called_once()

    def test_stop_is_idempotent(self, sandbox, mock_modal):
        sandbox.start()
        sandbox.stop()
        sandbox.stop()  # should not raise
        assert mock_modal["sandbox"].terminate.call_count == 1


class TestTransientRetry:
    def test_is_transient_detects_dns_and_connection_errors(self):
        import socket

        from vs_sandbox.modal_sandbox import _is_transient

        assert _is_transient(socket.gaierror(-2, "Name or service not known"))
        assert _is_transient(ConnectionResetError("Connection reset"))
        assert _is_transient(ConnectionRefusedError("conn refused"))
        assert _is_transient(Exception("gRPC UNAVAILABLE"))
        assert _is_transient(Exception("502 Bad Gateway"))
        assert _is_transient(Exception("Temporary failure in name resolution"))

        # Non-transient: real app errors should NOT retry.
        assert not _is_transient(ValueError("bad input"))
        assert not _is_transient(FileNotFoundError("/nowhere"))
        assert not _is_transient(RuntimeError("something specific"))

    def test_retry_recovers_after_transient_error(self, monkeypatch):
        import socket

        from vs_sandbox.modal_sandbox import _retry_transient

        # Avoid actual sleeping in the test.
        monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise socket.gaierror(-2, "Name or service not known")
            return "ok"

        assert _retry_transient(flaky, base_delay=0.01) == "ok"
        assert calls["n"] == 3

    def test_retry_reraises_non_transient_immediately(self):
        from vs_sandbox.modal_sandbox import _retry_transient

        calls = {"n": 0}

        def fatal():
            calls["n"] += 1
            raise ValueError("real bug")

        with pytest.raises(ValueError, match="real bug"):
            _retry_transient(fatal, base_delay=0.01)
        assert calls["n"] == 1  # no retries for non-transient

    def test_retry_gives_up_after_max_attempts(self, monkeypatch):
        import socket

        from vs_sandbox.modal_sandbox import _retry_transient

        monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)

        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            raise socket.gaierror(-2, "Name or service not known")

        with pytest.raises(socket.gaierror):
            _retry_transient(always_fails, max_attempts=3, base_delay=0.01)
        assert calls["n"] == 3

    def test_execute_retries_on_transient_then_succeeds(self, sandbox, mock_modal, monkeypatch):
        """Integration: a flaky sandbox.exec should be transparently retried."""
        import socket

        monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)
        sandbox.start()

        proc_ok = MagicMock()
        proc_ok.stdout.read.return_value = "success"
        proc_ok.stderr.read.return_value = ""
        proc_ok.wait.return_value = 0

        attempt = {"n": 0}

        def flaky_exec(*args, **kwargs):
            attempt["n"] += 1
            if attempt["n"] < 3:
                raise socket.gaierror(-2, "Name or service not known")
            return proc_ok

        mock_modal["sandbox"].exec.side_effect = flaky_exec

        resp = sandbox.execute("ls")
        assert resp.exit_code == 0
        assert "success" in resp.output
        assert attempt["n"] == 3  # retried twice, third succeeded


class TestSandboxFallbackRestart:
    """Verify the sandbox-dead fallback path: recreate + retry, preserving the volume."""

    def test_is_sandbox_dead_recognizes_shutdown_messages(self):
        from vs_sandbox.modal_sandbox import _is_sandbox_dead

        assert _is_sandbox_dead(RuntimeError("Sandbox has already shut down"))
        assert _is_sandbox_dead(Exception("Sandbox has exited"))
        assert _is_sandbox_dead(RuntimeError("sandbox is not running"))
        # Non-sandbox-dead errors must NOT trigger the restart path.
        import socket

        assert not _is_sandbox_dead(socket.gaierror(-2, "Name or service not known"))
        assert not _is_sandbox_dead(ValueError("bad argument"))

    def test_execute_restarts_sandbox_on_death_and_retries(
        self,
        sandbox,
        mock_modal,
        monkeypatch,
    ):
        """A dead sandbox should be recreated once, then the command re-run."""
        monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)
        sandbox.start()

        proc_ok = MagicMock()
        proc_ok.stdout.read.return_value = "after-restart-ok"
        proc_ok.stderr.read.return_value = ""
        proc_ok.wait.return_value = 0

        call = {"n": 0}

        def flaky_exec(*args, **kwargs):
            call["n"] += 1
            if call["n"] == 1:
                raise RuntimeError("Sandbox has already shut down")
            return proc_ok

        mock_modal["sandbox"].exec.side_effect = flaky_exec

        resp = sandbox.execute("echo hi")
        assert resp.exit_code == 0
        assert "after-restart-ok" in resp.output
        # 1 failing call (sandbox-dead) + init commands on restart + 1 successful call
        assert call["n"] >= 2

    def test_restart_attempts_capped_by_max(self, sandbox, mock_modal, monkeypatch):
        """After max_restart_attempts, further restarts are refused."""
        monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)
        sandbox._max_restart_attempts = 1
        sandbox.start()

        # First restart succeeds, a second attempted restart must be refused.
        assert sandbox._restart_sandbox() is True
        assert sandbox._restart_sandbox() is False  # capped

    def test_restart_can_be_disabled(self, tmp_path, mock_modal, monkeypatch):
        """enable_fallback_restart=False disables the recovery path entirely."""
        from vs_sandbox.modal_sandbox import ModalSandbox

        with ModalSandbox(
            host_workspace=str(tmp_path),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            enable_fallback_restart=False,
        ) as sb:
            monkeypatch.setattr("vs_sandbox.modal_sandbox.time.sleep", lambda _: None)

            call = {"n": 0}

            def dies(*args, **kwargs):
                call["n"] += 1
                raise RuntimeError("Sandbox has already shut down")

            mock_modal["sandbox"].exec.side_effect = dies

            resp = sb.execute("echo hi")
            assert resp.exit_code == -1
            assert "already shut down" in resp.output.lower()

    def test_extra_readonly_volumes_are_mounted(self, tmp_path, mock_modal):
        """Auxiliary volumes like /draft_model should be added to volumes dict."""
        import modal

        from vs_sandbox.modal_sandbox import ModalSandbox

        with ModalSandbox(
            host_workspace=str(tmp_path),
            image="nvcr.io/nvidia/pytorch:25.04-py3",
            extra_readonly_volumes={"/draft_model": "vibesys-model-eagle3"},
        ):
            kwargs = modal.Sandbox.create.call_args.kwargs
            assert "/draft_model" in kwargs["volumes"]


class TestPathOverrides:
    def test_read_translates_virtual_path(self, sandbox, mock_modal):
        sandbox.start()
        mock_modal["proc"].stdout.read.return_value = "     1\thello\n"
        sandbox.read("/foo.txt")
        # The base class builds a python3 heredoc command — check /workspace/foo.txt appears
        # (it gets base64-encoded; easier to check via _vpath directly).
        assert sandbox._vpath("/foo.txt") == "/workspace/foo.txt"
