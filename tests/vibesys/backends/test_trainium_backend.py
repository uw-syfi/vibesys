"""Tests for the Trainium (AWS NeuronCore) backend."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

from tests.support import capture_docker_start_argv

from entrypoints.cli import _add_common_args
from vibesys import backends
from vibesys.backends import SandboxKind
from vibesys.backends.trainium import TrainiumBackend
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vs_sandbox.api import DockerSandbox, HostResource, HostResourceAccess, LocalShellSandbox

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def _make_backend(tmp_path: Path, devices: Iterable[str] = ("/dev/neuron0",)) -> TrainiumBackend:
    with patch("vibesys.backends.trainium._discover_neuron_devices", return_value=list(devices)):
        impl = backends.get(ComputeBackend.TRAINIUM, log_dir=tmp_path / "logs")
    assert isinstance(impl, TrainiumBackend)
    return impl


class TestTrainiumRegistry:
    def test_trainium_in_registry(self, tmp_path: Path) -> None:
        impl = backends.get(ComputeBackend.TRAINIUM, log_dir=tmp_path)
        assert isinstance(impl, TrainiumBackend)
        assert impl.name is ComputeBackend.TRAINIUM
        assert impl.profiler_kind is ProfilerKind.NEURON


class TestTrainiumSandbox:
    def test_local_returns_local_shell_backend(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(workspace),
            log_path=None,
            extra_env={"FOO": "bar"},
        )
        assert isinstance(sb, LocalShellSandbox)

    def test_docker_forwards_neuron_devices_and_no_gpus(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path, devices=["/dev/neuron0", "/dev/neuron1"])
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)

        argv = capture_docker_start_argv(sb)
        device_args = [
            argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "--device"
        ]
        mounts = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-v"]
        env = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-e"]
        assert device_args == ["/dev/neuron0", "/dev/neuron1"]
        assert "--gpus" not in argv
        assert "--rm" in argv
        assert argv[argv.index("--entrypoint") + 1] == ""
        assert argv[argv.index("--shm-size") + 1] == "16g"
        assert any(mount.endswith(":/opt/neuron-compile-cache") for mount in mounts)
        assert any(mount.endswith(":/opt/neuron-tmp") for mount in mounts)
        assert "NEURON_COMPILE_CACHE_URL=/opt/neuron-compile-cache" in env
        assert "TMPDIR=/opt/neuron-tmp" in env

    def test_docker_forwards_resources_to_the_sandbox(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        resource = HostResource(tmp_path / "history", HostResourceAccess.READ_ONLY, "history")

        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
            resources=[resource],
        )

        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        mounts = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-v"]
        assert any(
            mount.startswith(f"{resource.path}:") and mount.endswith(":ro") for mount in mounts
        )


class TestTrainiumDevice:
    def test_no_monitor(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        assert impl.make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        impl.reselect_device()
        assert impl.selected_device is None


class TestTrainiumCli:
    def test_argparse_accepts_trainium(self) -> None:
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "trainium"])
        assert ns.backend is ComputeBackend.TRAINIUM
