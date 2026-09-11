"""ROCm backend: AMD Instinct GPUs + ROCm PyTorch container + torch profiler.

ROCm shares the discrete-accelerator model with CUDA — separate device
memory, dynamic shapes, per-kernel launch cost — so the serving techniques
carry over. What differs is plumbing:

* AMD GPUs are exposed as ``/dev/kfd`` (the compute driver) plus
  ``/dev/dri/*`` render nodes, forwarded with ``docker --device`` rather
  than the NVIDIA-only ``--gpus`` flag. Container users additionally need
  the ``video`` and ``render`` groups.
* The runtime ships in the ROCm PyTorch image (``rocm/pytorch``).
* Device selection uses ``HIP_VISIBLE_DEVICES`` and ``rocm-smi``.
* Profiling uses ``torch.profiler``, which works unmodified on ROCm.
  ``rocprofv3`` / ``omniperf`` are the system- and kernel-altitude tools
  but are not wired as a dedicated :class:`ProfilerKind` yet.

.. warning::

   **Experimental.** This backend is wired end to end but has not been
   exercised against MI300-class hardware in this repository. Device
   discovery and the container contract follow the documented ROCm
   conventions; treat them as unverified until a run confirms them. Like
   :class:`~vibesys.backends.local.LocalBackend`'s ``metal`` and ``cpu``
   bindings; serving-domain prompts may also need target-specific adaptation.

There is no remote-GPU sandbox path: ``make_sandbox`` supports only
``SandboxKind.LOCAL`` and ``SandboxKind.DOCKER`` (parity with the Trainium
backend).
"""

from __future__ import annotations

import glob
import os
import subprocess
from collections.abc import Callable, Sequence  # noqa: TC003  # tracked: #288
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.backends.base import (
    ContentionMonitor,
    SandboxKind,
    make_local_shell_sandbox,
)
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind

if TYPE_CHECKING:
    # Annotation only; deepagents pulls langchain + anthropic (~seconds).
    from deepagents.backends.protocol import SandboxBackendProtocol

    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks

# ROCm PyTorch image. Carries the ROCm runtime + a matching torch build.
# Pinned rather than ``:latest`` for reproducibility and because the
# container's ROCm must stay compatible with the host kernel driver —
# a floating tag is the one most likely to drift past it. Override with
# ``--docker-image`` when the host ROCm version differs.
_DEFAULT_IMAGE = "rocm/pytorch:rocm6.3_ubuntu22.04_py3.10_pytorch_release_2.4.0"

# PyTorch wheel index matching the image's ROCm. The agent is instructed to
# use ``uv add torch`` in a fresh venv, which is isolated from the image's
# site-packages; without this it resolves the default PyPI wheel, which is a
# CUDA build. On an AMD host that silently yields ``torch.cuda.is_available()
# == False`` and a CPU fallback — a wrong-hardware run that looks like a
# correctness failure. Mirrors CudaBackend's driver-matched index.
_TORCH_INDEX_URL = "https://download.pytorch.org/whl/rocm6.3"

# The compute driver node, required for any HIP program.
_KFD_DEVICE = "/dev/kfd"

# Docker's default 64 MB /dev/shm is too small for multi-GPU collectives
# (RCCL) and large-model loading.
_DEFAULT_SHM_SIZE = "16g"

# Container groups needed to open /dev/kfd and /dev/dri nodes.
_DEVICE_GROUPS: tuple[str, ...] = ("video", "render")


def _discover_rocm_devices() -> list[str]:
    """Return the host's AMD GPU device nodes, sorted.

    ``/dev/kfd`` is the compute driver and is required; ``/dev/dri/render*``
    nodes are the per-GPU render devices. Returns an empty list when the
    host has no AMD GPU, in which case the container starts without an
    accelerator (parity with the Trainium backend's behaviour).
    """
    if not os.path.exists(_KFD_DEVICE):  # noqa: PTH110  # tracked: #288
        return []
    render_nodes = sorted(glob.glob("/dev/dri/render*"))  # noqa: PTH207  # tracked: #288
    return [_KFD_DEVICE, *render_nodes]


def _query_rocm_gpu_count() -> int | None:
    """Return the number of GPUs ``rocm-smi`` reports, or None if unavailable."""
    try:
        result = subprocess.run(  # noqa: PLW1510  # tracked: #288
            ["rocm-smi", "--showid", "--csv"],  # noqa: S607  # tracked: #288
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # CSV: header row then one row per device.
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    return max(len(rows) - 1, 0)


class RocmBackend:
    """AMD ROCm backend (local or Docker; no Modal).

    Experimental — see the module docstring.
    """

    name = ComputeBackend.ROCM
    profiler_kind = ProfilerKind.TORCH

    def __init__(  # noqa: D107  # tracked: #288
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image or _DEFAULT_IMAGE
        # No per-device auto-selection yet; pinning is via HIP_VISIBLE_DEVICES.
        # Kept for ComputeBackendImpl parity.
        self.selected_device = None
        self._devices = _discover_rocm_devices()

        if self._devices:
            count = _query_rocm_gpu_count()
            detail = f"{count} GPU(s), " if count is not None else ""
            self._lprint(
                f"[rocm] {detail}forwarding {len(self._devices)} device node(s): "
                f"{', '.join(self._devices)}"
            )
        else:
            self._lprint(
                "[rocm] No /dev/kfd found on host — the container will start "
                "without an accelerator."
            )

    # -- ComputeBackendImpl protocol ---------------------------------------

    def make_sandbox(  # noqa: D102, PLR0913  # tracked: #288
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
    ) -> SandboxBackendProtocol:
        # Deferred: the sandbox classes subclass deepagents' BaseSandbox, which
        # pulls langchain + anthropic. Registration must stay import-cheap.
        from vs_sandbox import DockerSandbox  # noqa: PLC0415  # tracked: #288

        bind_mounts = list(bind_mounts or [])
        passthrough_paths = list(passthrough_paths or [])
        extra_env = dict(extra_env or {})
        lifecycle_hooks = lifecycle_hooks or []
        # Accepted for ComputeBackendImpl protocol parity but unused: neither
        # the LOCAL sandbox nor the agent-image-based DOCKER sandbox runs
        # per-launch install commands.
        del ephemeral, extra_init_commands

        env = self._build_env(extra_env)

        if kind is SandboxKind.LOCAL:
            return make_local_shell_sandbox(
                host_workspace=host_workspace,
                env=env,
                lifecycle_hooks=lifecycle_hooks,
            )

        if kind is SandboxKind.DOCKER:
            return DockerSandbox(
                host_workspace=host_workspace,
                image=container_image or self.image,
                gpus=None,  # ROCm uses --device, not --gpus
                devices=self._devices if attach_accelerator else [],
                group_add=list(_DEVICE_GROUPS),
                shm_size=_DEFAULT_SHM_SIZE,
                bind_mounts=bind_mounts,
                resources=resources,
                passthrough_paths=passthrough_paths,
                env=env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
            )

        raise ValueError(f"Unknown sandbox kind: {kind!r}")  # noqa: TRY003  # tracked: #288

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:  # noqa: ARG002, D102  # tracked: #288
        # rocm-smi can report utilization, but shared-device contention
        # handling isn't wired up yet; skip rather than fake it.
        return None

    def reselect_device(self) -> None:  # noqa: D102  # tracked: #288
        return None

    # -- internal ----------------------------------------------------------

    def _build_env(self, extra: dict[str, str]) -> dict[str, str]:
        """ROCm runtime env, with caller extras taking precedence."""
        # ``uv add torch`` must resolve a ROCm wheel, not the default CUDA one.
        env: dict[str, str] = {"UV_EXTRA_INDEX_URL": _TORCH_INDEX_URL}
        # Respect an operator-pinned device selection; otherwise leave the
        # runtime to enumerate every forwarded GPU.
        visible = os.environ.get("HIP_VISIBLE_DEVICES")
        if visible:
            env["HIP_VISIBLE_DEVICES"] = visible
        env.update(extra)
        return env
