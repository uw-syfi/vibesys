"""Tests for the Metal (Apple Silicon) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from vibeserve_agent import backends
from vibeserve_agent.backends import SandboxKind
from vibeserve_agent.backends.metal import MetalBackend
from vibeserve_agent.cli import _add_common_args
from vibeserve_agent.constants import ComputeBackend


def _make_backend(tmp_path) -> MetalBackend:
    return backends.get(ComputeBackend.METAL, log_dir=tmp_path / "logs")


class TestMetalRegistry:
    def test_metal_in_registry(self, tmp_path):
        impl = backends.get(ComputeBackend.METAL, log_dir=tmp_path)
        assert isinstance(impl, MetalBackend)
        assert impl.name is ComputeBackend.METAL
        assert impl.profiler_kind == "torch"


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
        with pytest.raises(ValueError, match="local execution"):
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
