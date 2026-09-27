"""Tests for the ROCm (AMD Instinct) backend."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from tests.support import capture_docker_start_argv

from entrypoints.cli import _add_common_args
from vibesys.orchestration.prompts import PROMPTS_DIR, RocmComputeBackendFragment
from vibesys.orchestration.prompts.renderer import _FRAGMENT_IMPLS, ComputeBackendFragment
from vs_sandbox.api import (
    AcceleratorInventory,
    ComputeBackend,
    DockerSandbox,
    HostResource,
    HostResourceAccess,
    LocalShellSandbox,
    RocmBackend,
    SandboxKind,
    create_compute_backend,
)
from vs_sandbox.api.testing import FakeAcceleratorDiscovery

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    import pytest


def _make_backend(
    tmp_path: Path, devices: Iterable[str] = ("/dev/kfd", "/dev/dri/renderD128")
) -> RocmBackend:
    return RocmBackend(
        tmp_path / "logs",
        accelerator_discovery=FakeAcceleratorDiscovery(
            rocm=AcceleratorInventory(tuple(devices), reported_device_count=1)
        ),
    )


class TestRocmRegistry:
    def test_rocm_in_registry(self, tmp_path: Path) -> None:
        impl = create_compute_backend(ComputeBackend.ROCM, log_dir=tmp_path)
        assert isinstance(impl, RocmBackend)
        assert impl.name is ComputeBackend.ROCM
        # rocprofv3 / rocprof-compute is the dedicated system- and
        # kernel-altitude toolkit for ROCm; torch.profiler is still
        # selectable via --profiler torch.


class TestRocmSandbox:
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

    def test_docker_forwards_kfd_and_dri_without_gpus_flag(self, tmp_path: Path) -> None:
        """AMD GPUs come in via --device, not the NVIDIA-only --gpus."""
        impl = _make_backend(tmp_path, devices=["/dev/kfd", "/dev/dri/renderD128"])
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        devices = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "--device"]
        assert devices == ["/dev/kfd", "/dev/dri/renderD128"]
        assert "--gpus" not in argv

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

    def test_docker_can_skip_accelerator_for_control_plane(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
            attach_accelerator=False,
        )

        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        assert "--device" not in argv

    def test_docker_adds_device_groups(self, tmp_path: Path) -> None:
        """/dev/kfd and /dev/dri/* are group-owned; without these the container
        user cannot open them and every HIP call fails at runtime."""
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        groups = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "--group-add"]
        assert groups == ["video", "render"]

    def test_torch_wheel_index_targets_rocm(self, tmp_path: Path) -> None:
        """Without this, `uv add torch` in the agent's fresh venv resolves the
        default PyPI (CUDA) wheel and the run silently falls back to CPU."""
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        env = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-e"]
        assert any(value.startswith("UV_EXTRA_INDEX_URL=") and "rocm" in value for value in env)

    def test_default_image_is_pinned(self, tmp_path: Path) -> None:
        """A floating :latest tag can drift past the host kernel driver."""

        assert not _make_backend(tmp_path).image.endswith(":latest")

    def test_hip_visible_devices_is_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIP_VISIBLE_DEVICES", "2")
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        env = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-e"]
        assert "HIP_VISIBLE_DEVICES=2" in env

    def test_caller_env_overrides_backend_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIP_VISIBLE_DEVICES", "2")
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(workspace),
            log_path=None,
            extra_env={"HIP_VISIBLE_DEVICES": "0"},
        )
        assert isinstance(sb, DockerSandbox)
        argv = capture_docker_start_argv(sb)
        env = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-e"]
        assert "HIP_VISIBLE_DEVICES=0" in env


class TestRocmDevice:
    def test_no_monitor(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        assert impl.make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path: Path) -> None:
        impl = _make_backend(tmp_path)
        impl.reselect_device()
        assert impl.selected_device is None


class TestRocmCli:
    def test_argparse_accepts_rocm(self) -> None:
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "rocm"])
        assert ns.backend is ComputeBackend.ROCM


class TestRocmPromptFragments:
    def test_every_fragment_name_exists_for_rocm(self) -> None:
        """`Prompt.__init__` calls validate(); a missing .j2 fails the run."""
        RocmComputeBackendFragment.validate()

    def test_rocm_is_registered_in_the_fragment_impl_table(self) -> None:
        """An unregistered backend raises at prompt construction time."""
        assert _FRAGMENT_IMPLS[ComputeBackend.ROCM] is RocmComputeBackendFragment

    def test_every_backend_has_a_fragment_impl(self) -> None:
        """Adding a ComputeBackend without fragments breaks every run on it."""
        assert set(_FRAGMENT_IMPLS) == set(ComputeBackend)

    def test_rocm_fragments_are_non_empty(self) -> None:
        """Empty .j2 is a legal 'hard skip', but ROCm has real content for all
        three — an accidental empty file would silently drop prompt guidance."""
        backend_dir = PROMPTS_DIR / "backend" / ComputeBackend.ROCM.value
        for name in ComputeBackendFragment.NAMES:
            assert (backend_dir / f"{name}.j2").read_text().strip()
