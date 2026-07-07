"""Tests for the CPU (no-GPU) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from vibe_serve import backends
from vibe_serve.backends import SandboxKind
from vibe_serve.backends.local import LocalBackend
from vibe_serve.cli import _add_common_args
from vibe_serve.constants import ComputeBackend
from vibe_serve.sandbox.docker_sandbox import DockerSandbox


def _make_backend(tmp_path) -> LocalBackend:
    return backends.get(ComputeBackend.CPU, log_dir=tmp_path / "logs")


class TestCpuRegistry:
    def test_cpu_in_registry(self, tmp_path):
        impl = backends.get(ComputeBackend.CPU, log_dir=tmp_path)
        assert isinstance(impl, LocalBackend)
        assert impl.name is ComputeBackend.CPU
        assert impl.profiler_kind == "torch"


class TestCpuSandbox:
    def test_local_returns_local_shell_backend(self, tmp_path):
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(workspace),
            log_path=None,
            extra_env={"FOO": "bar"},
        )
        assert isinstance(sb, LocalShellBackend)

    def test_docker_returns_docker_sandbox_without_gpus(self, tmp_path):
        impl = _make_backend(tmp_path)
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(tmp_path),
            log_path=None,
            extra_env={"FOO": "bar"},
        )
        assert isinstance(sb, DockerSandbox)
        assert sb._gpus is None
        assert sb._image == impl.image
        assert sb._env["FOO"] == "bar"

    def test_modal_raises(self, tmp_path):
        impl = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="Modal CPU execution is not wired up"):
            impl.make_sandbox(
                SandboxKind.MODAL,
                host_workspace=str(tmp_path),
                log_path=None,
            )


class TestCpuDevice:
    def test_no_monitor(self, tmp_path):
        assert _make_backend(tmp_path).make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path):
        impl = _make_backend(tmp_path)
        # No-op: doesn't raise, doesn't change selected_device.
        impl.reselect_device()
        assert impl.selected_device is None


class TestCpuCli:
    def test_argparse_accepts_cpu(self):
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "cpu"])
        assert ns.backend is ComputeBackend.CPU
