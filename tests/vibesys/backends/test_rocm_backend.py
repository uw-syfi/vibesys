"""Tests for the ROCm (AMD Instinct) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from entrypoints.headless import _add_common_args
from vibesys import backends
from vibesys.backends import SandboxKind
from vibesys.backends.rocm import RocmBackend, _discover_rocm_devices
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, RocmComputeBackendFragment
from vibesys.prompts.renderer import _FRAGMENT_IMPLS, ComputeBackendFragment
from vs_sandbox import DockerSandbox, HostResource, HostResourceAccess


def _make_backend(tmp_path, devices=("/dev/kfd", "/dev/dri/renderD128")) -> RocmBackend:  # noqa: ANN001  # tracked: #288
    impl = backends.get(ComputeBackend.ROCM, log_dir=tmp_path / "logs")
    assert isinstance(impl, RocmBackend)
    # Pin a deterministic device set so tests don't depend on host hardware.
    impl._devices = list(devices)  # noqa: SLF001  # tracked: #288
    return impl


class TestRocmRegistry:
    def test_rocm_in_registry(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = backends.get(ComputeBackend.ROCM, log_dir=tmp_path)
        assert isinstance(impl, RocmBackend)
        assert impl.name is ComputeBackend.ROCM
        # torch.profiler works on ROCm, so no dedicated profiler kind is needed.
        assert impl.profiler_kind is ProfilerKind.TORCH


class TestRocmSandbox:
    def test_local_returns_local_shell_backend(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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

    def test_docker_forwards_kfd_and_dri_without_gpus_flag(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert sb._devices == ["/dev/kfd", "/dev/dri/renderD128"]  # noqa: SLF001  # tracked: #288
        assert sb._gpus is None  # noqa: SLF001  # tracked: #288

    def test_docker_forwards_resources_to_the_sandbox(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert resource in sb._resources  # noqa: SLF001  # tracked: #288

    def test_docker_can_skip_accelerator_for_control_plane(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert sb._devices == []  # noqa: SLF001  # tracked: #288

    def test_docker_adds_device_groups(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert sb._group_add == ["video", "render"]  # noqa: SLF001  # tracked: #288

    def test_modal_raises(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        """Modal has no AMD GPUs — fail loudly rather than silently on CPU."""
        impl = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="does not support Modal"):
            impl.make_sandbox(
                SandboxKind.MODAL,
                host_workspace=str(tmp_path),
                log_path=None,
            )

    def test_torch_wheel_index_targets_rocm(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert "rocm" in sb._env["UV_EXTRA_INDEX_URL"]  # noqa: SLF001  # tracked: #288

    def test_default_image_is_pinned(self):  # noqa: ANN201  # tracked: #288
        """A floating :latest tag can drift past the host kernel driver."""
        from vibesys.backends.rocm import _DEFAULT_IMAGE  # noqa: PLC0415  # tracked: #288

        assert not _DEFAULT_IMAGE.endswith(":latest")

    def test_hip_visible_devices_is_respected(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert sb._env.get("HIP_VISIBLE_DEVICES") == "2"  # noqa: SLF001  # tracked: #288

    def test_caller_env_overrides_backend_env(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
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
        assert sb._env.get("HIP_VISIBLE_DEVICES") == "0"  # noqa: SLF001  # tracked: #288


class TestRocmDeviceDiscovery:
    def test_no_kfd_means_no_devices(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN201, ARG002  # tracked: #288
        """A host without /dev/kfd yields an empty list rather than raising."""
        monkeypatch.setattr("vibesys.backends.rocm.os.path.exists", lambda _p: False)
        assert _discover_rocm_devices() == []

    def test_kfd_leads_and_render_nodes_follow(self, monkeypatch):  # noqa: ANN001, ANN201  # tracked: #288
        monkeypatch.setattr("vibesys.backends.rocm.os.path.exists", lambda _p: True)
        monkeypatch.setattr(
            "vibesys.backends.rocm.glob.glob",
            lambda _pat: ["/dev/dri/renderD129", "/dev/dri/renderD128"],
        )
        assert _discover_rocm_devices() == [
            "/dev/kfd",
            "/dev/dri/renderD128",
            "/dev/dri/renderD129",
        ]


class TestRocmDevice:
    def test_no_monitor(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = _make_backend(tmp_path)
        assert impl.make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = _make_backend(tmp_path)
        impl.reselect_device()
        assert impl.selected_device is None


class TestRocmCli:
    def test_argparse_accepts_rocm(self):  # noqa: ANN201  # tracked: #288
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "rocm"])
        assert ns.backend is ComputeBackend.ROCM


class TestRocmPromptFragments:
    def test_every_fragment_name_exists_for_rocm(self):  # noqa: ANN201  # tracked: #288
        """`Prompt.__init__` calls validate(); a missing .j2 fails the run."""
        RocmComputeBackendFragment.validate()

    def test_rocm_is_registered_in_the_fragment_impl_table(self):  # noqa: ANN201  # tracked: #288
        """An unregistered backend raises at prompt construction time."""
        assert _FRAGMENT_IMPLS[ComputeBackend.ROCM] is RocmComputeBackendFragment

    def test_every_backend_has_a_fragment_impl(self):  # noqa: ANN201  # tracked: #288
        """Adding a ComputeBackend without fragments breaks every run on it."""
        assert set(_FRAGMENT_IMPLS) == set(ComputeBackend)

    def test_rocm_fragments_are_non_empty(self):  # noqa: ANN201  # tracked: #288
        """Empty .j2 is a legal 'hard skip', but ROCm has real content for all
        three — an accidental empty file would silently drop prompt guidance."""
        backend_dir = PROMPTS_DIR / "backend" / ComputeBackend.ROCM.value
        for name in ComputeBackendFragment.NAMES:
            assert (backend_dir / f"{name}.j2").read_text().strip()
