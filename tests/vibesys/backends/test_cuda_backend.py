from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibesys.backends import SandboxKind
from vibesys.backends.cuda import CudaBackend
from vs_sandbox import DockerSandbox, HostResource, HostResourceAccess


def test_cpu_only_control_plane_docker_skips_gpu_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CudaBackend(tmp_path)
    pick_device = MagicMock()
    monkeypatch.setattr(backend, "_pick_device", pick_device)

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
    )

    pick_device.assert_not_called()
    assert isinstance(sandbox, DockerSandbox)
    assert sandbox._gpus is None  # noqa: SLF001  # tracked: #288
    assert backend.selected_device is None


def test_ephemeral_setup_sandbox_is_not_tracked_for_reselection(tmp_path: Path) -> None:
    backend = CudaBackend(tmp_path)

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
        ephemeral=True,
    )

    assert isinstance(sandbox, DockerSandbox)
    assert backend._sandboxes == []  # noqa: SLF001


def test_docker_forwards_resources_to_the_sandbox(tmp_path: Path) -> None:
    backend = CudaBackend(tmp_path)
    resource = HostResource(tmp_path / "history", HostResourceAccess.READ_ONLY, "history")

    sandbox = backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path),
        log_path=None,
        attach_accelerator=False,
        resources=[resource],
    )

    assert isinstance(sandbox, DockerSandbox)
    assert resource in sandbox._resources  # noqa: SLF001  # tracked: #288
