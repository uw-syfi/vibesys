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

import os
import shutil
import subprocess
from importlib import import_module
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
    from collections.abc import Callable, Sequence

    from vs_sandbox.api import HostResource, Sandbox, SandboxLifecycleHooks

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
    if not Path(_KFD_DEVICE).exists():
        return []
    render_nodes = sorted(str(path) for path in Path("/dev/dri").glob("render*"))
    return [_KFD_DEVICE, *render_nodes]


def _query_rocm_gpu_count() -> int | None:
    """Return the number of GPUs ``rocm-smi`` reports, or None if unavailable."""
    rocm_smi = shutil.which("rocm-smi")
    if rocm_smi is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-010233 [S603]; run the resolved ROCm status utility with fixed read-only arguments.
            [rocm_smi, "--showid", "--csv"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
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

    def __init__(
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        """Configure ROCm execution with its log directory and image override."""
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

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-011113 [PLR0913]; RunEnvironment dispatches this structural ComputeBackendImpl method with shared named sandbox options; changing it would break backend parity.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
    ) -> Sandbox:
        """Create a local or ROCm-enabled Docker sandbox."""
        # Deferred: importing DockerSandbox registers process-wide signal and
        # atexit handlers. Registration must stay side-effect free.
        docker_sandbox = import_module("vs_sandbox.api").DockerSandbox

        bind_mounts = list(bind_mounts or [])
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
            return docker_sandbox(
                host_workspace=host_workspace,
                image=container_image or self.image,
                gpus=None,  # ROCm uses --device, not --gpus
                devices=self._devices if attach_accelerator else [],
                group_add=list(_DEVICE_GROUPS),
                shm_size=_DEFAULT_SHM_SIZE,
                bind_mounts=bind_mounts,
                resources=resources,
                env=env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
            )

        message = f"Unknown sandbox kind: {kind!r}"
        raise ValueError(message)

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Return no monitor until ROCm contention handling is available."""
        del log_dir
        # rocm-smi can report utilization, but shared-device contention
        # handling isn't wired up yet; skip rather than fake it.
        return None

    def reselect_device(self) -> None:
        """Do nothing because ROCm device reselection is not implemented."""
        return

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
