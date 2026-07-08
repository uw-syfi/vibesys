"""Tests for the Trainium (AWS NeuronCore) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from vibe_serve import backends
from vibe_serve.backends import SandboxKind
from vibe_serve.backends.trainium import TrainiumBackend
from vibe_serve.cli import _add_common_args
from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind


def _make_backend(tmp_path, devices=("/dev/neuron0",)) -> TrainiumBackend:
    impl = backends.get(ComputeBackend.TRAINIUM, log_dir=tmp_path / "logs")
    # Pin a deterministic device set so tests don't depend on host hardware.
    impl._devices = list(devices)
    return impl


class TestTrainiumRegistry:
    def test_trainium_in_registry(self, tmp_path):
        impl = backends.get(ComputeBackend.TRAINIUM, log_dir=tmp_path)
        assert isinstance(impl, TrainiumBackend)
        assert impl.name is ComputeBackend.TRAINIUM
        assert impl.profiler_kind is ProfilerKind.NEURON


class TestTrainiumSandbox:
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

    def test_docker_forwards_neuron_devices_and_no_gpus(self, tmp_path):
        impl = _make_backend(tmp_path, devices=["/dev/neuron0", "/dev/neuron1"])
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        # DockerSandbox stores the device list and skips --gpus.
        assert sb._devices == ["/dev/neuron0", "/dev/neuron1"]
        assert sb._gpus is None
        # Persistent compile cache is bind-mounted and passthrough-registered.
        assert any(
            container == "/opt/neuron-compile-cache" for _host, container, _ro in sb._bind_mounts
        )
        assert "/opt/neuron-compile-cache" in sb._passthrough_prefixes
        assert sb._env.get("NEURON_COMPILE_CACHE_URL") == "/opt/neuron-compile-cache"
        # auto-remove container + host-mounted neuronx-cc temp (TMPDIR)
        assert sb._auto_remove is True
        assert sb._env.get("TMPDIR") == "/opt/neuron-tmp"
        assert any(c == "/opt/neuron-tmp" for _h, c, _ro in sb._bind_mounts)

    def test_modal_raises(self, tmp_path):
        impl = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="does not support Modal"):
            impl.make_sandbox(
                SandboxKind.MODAL,
                host_workspace=str(tmp_path),
                log_path=None,
            )


class TestTrainiumDevice:
    def test_no_monitor(self, tmp_path):
        impl = _make_backend(tmp_path)
        assert impl.make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path):
        impl = _make_backend(tmp_path)
        impl.reselect_device()
        assert impl.selected_device is None


class TestTrainiumCli:
    def test_argparse_accepts_trainium(self):
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "trainium"])
        assert ns.backend is ComputeBackend.TRAINIUM
