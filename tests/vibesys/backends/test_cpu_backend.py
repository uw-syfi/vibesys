"""Tests for the CPU (no-GPU) backend."""

from __future__ import annotations

import argparse

import pytest
from deepagents.backends import LocalShellBackend

from entrypoints.headless import _add_common_args
from vibesys import backends
from vibesys.backends import SandboxKind
from vibesys.backends.local import LocalBackend
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vs_sandbox import BeforeReadyContext, DockerSandbox, SandboxLifecycleHooks


class _RecordingHooks(SandboxLifecycleHooks):
    def __init__(self) -> None:
        self.sandbox: object | None = None

    def before_ready(self, context: BeforeReadyContext) -> None:
        self.sandbox = context.sandbox


def _make_backend(tmp_path) -> LocalBackend:  # noqa: ANN001  # tracked: #288
    impl = backends.get(ComputeBackend.CPU, log_dir=tmp_path / "logs")
    assert isinstance(impl, LocalBackend)
    return impl


class TestCpuRegistry:
    def test_cpu_in_registry(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = backends.get(ComputeBackend.CPU, log_dir=tmp_path)
        assert isinstance(impl, LocalBackend)
        assert impl.name is ComputeBackend.CPU
        assert impl.profiler_kind is ProfilerKind.LINUX_CPU


class TestCpuSandbox:
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

    def test_local_runs_lifecycle_hooks_before_returning(self, tmp_path):  # noqa: ANN001, ANN201
        impl = _make_backend(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        hooks = _RecordingHooks()

        sb = impl.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(workspace),
            log_path=None,
            lifecycle_hooks=[hooks],
        )

        assert hooks.sandbox is sb

    def test_docker_returns_docker_sandbox_without_gpus(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = _make_backend(tmp_path)
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(tmp_path),
            log_path=None,
            extra_env={"FOO": "bar"},
            container_image="sha256:" + "a" * 64,
        )
        assert isinstance(sb, DockerSandbox)
        assert sb._gpus is None  # noqa: SLF001  # tracked: #288
        assert sb._image == "sha256:" + "a" * 64  # noqa: SLF001  # tracked: #288
        assert sb._env["FOO"] == "bar"  # noqa: SLF001  # tracked: #288

    def test_docker_falls_back_to_the_backend_image_without_a_resolved_container_image(  # noqa: ANN201  # tracked: #288
        self,
        tmp_path,  # noqa: ANN001  # tracked: #288
    ):
        """A caller that builds its own Docker sandbox without one still works.

        ``DockerEnvironment.open()`` (the plain ``--docker`` path) always
        resolves an agent image and passes it as ``container_image``, so in
        practice this fallback is dead there; it still matters for Modal and
        SkyPilot's CPU-only local editor container, which builds no agent
        image and never passes ``container_image``.
        """
        impl = _make_backend(tmp_path)
        sb = impl.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(tmp_path),
            log_path=None,
        )
        assert isinstance(sb, DockerSandbox)
        assert sb._image == impl.image  # noqa: SLF001  # tracked: #288

    def test_modal_raises(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = _make_backend(tmp_path)
        with pytest.raises(ValueError, match="Modal CPU execution is not wired up"):
            impl.make_sandbox(
                SandboxKind.MODAL,
                host_workspace=str(tmp_path),
                log_path=None,
            )


class TestCpuDevice:
    def test_no_monitor(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        assert _make_backend(tmp_path).make_monitor(tmp_path) is None

    def test_reselect_is_noop(self, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        impl = _make_backend(tmp_path)
        # No-op: doesn't raise, doesn't change selected_device.
        impl.reselect_device()
        assert impl.selected_device is None


class TestCpuCli:
    def test_argparse_accepts_cpu(self):  # noqa: ANN201  # tracked: #288
        parser = argparse.ArgumentParser()
        _add_common_args(parser)
        ns = parser.parse_args(["--backend", "cpu"])
        assert ns.backend is ComputeBackend.CPU
