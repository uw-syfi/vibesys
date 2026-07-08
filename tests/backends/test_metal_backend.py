"""Tests for the Metal (Apple Silicon) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from vibe_serve import backends
from vibe_serve.backends import SandboxKind
from vibe_serve.backends.local import LocalBackend
from vibe_serve.cli import _add_common_args
from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind


def _make_backend(tmp_path) -> LocalBackend:
    return backends.get(ComputeBackend.METAL, log_dir=tmp_path / "logs")


class TestMetalRegistry:
    def test_metal_in_registry(self, tmp_path):
        impl = backends.get(ComputeBackend.METAL, log_dir=tmp_path)
        assert isinstance(impl, LocalBackend)
        assert impl.name is ComputeBackend.METAL
        assert impl.profiler_kind is ProfilerKind.TORCH


class TestMetalSandbox:
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

    def test_docker_raises(self, tmp_path):
        impl = _make_backend(tmp_path)
        # Metal-specific identity: message names metal + the MPS/Apple reason.
        with pytest.raises(ValueError, match="metal backend only supports local execution"):
            impl.make_sandbox(
                SandboxKind.DOCKER,
                host_workspace=str(tmp_path),
                log_path=None,
            )

    def test_modal_raises(self, tmp_path):
        impl = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="local execution"):
            impl.make_sandbox(
                SandboxKind.MODAL,
                host_workspace=str(tmp_path),
                log_path=None,
            )


class TestMetalDevice:
    def test_no_monitor(self, tmp_path):
        impl = _make_backend(tmp_path)
        assert impl.make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path):
        impl = _make_backend(tmp_path)
        # No-op: doesn't raise, doesn't change selected_device.
        impl.reselect_device()
        assert impl.selected_device is None


class TestMetalCli:
    def test_argparse_accepts_metal(self):
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "metal"])
        assert ns.backend is ComputeBackend.METAL
