"""CUDA backend: NVIDIA GPU + nvcr.io PyTorch container + nsys profiler."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.sandbox import BaseSandbox

from vibe_serve.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    SetupFn,
)
from vibe_serve.backends.cuda.gpu_monitor import (
    GpuContentionMonitor,
    GpuInfo,
    pick_gpu,
    query_gpu_info,
)
from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind
from vibe_serve.sandbox.docker_sandbox import DockerSandbox
from vibe_serve.sandbox.modal_sandbox import ModalSandbox

# Default container image for the cuda backend.  Carries CUDA toolkit + PyTorch.
_DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:25.04-py3"


class CudaBackend:
    """CUDA / NVIDIA backend.

    Picks the least-loaded GPU via ``nvidia-smi``, pins it for the run, and
    bakes the right env (CUDA_VISIBLE_DEVICES + a CUDA-driver-matched PyTorch
    wheel index) into every sandbox it constructs.
    """

    name = ComputeBackend.CUDA
    profiler_kind = ProfilerKind.NSYS

    def __init__(
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image or _DEFAULT_IMAGE
        self.selected_device: GpuInfo | None = None
        self._monitor: GpuContentionMonitor | None = None
        # Sandboxes built so far, so maybe_rebalance can find them without
        # the caller having to thread them back in.
        # (kind, sandbox) tuples — kind is recorded at registration time so
        # ``reselect_device`` can dispatch without isinstance checks against
        # cross-package types (which break test mocking).
        self._sandboxes: list[tuple[SandboxKind, BaseSandbox]] = []

    # -- ComputeBackendImpl protocol ---------------------------------------------

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        setup_fns: list[SetupFn] | None = None,
        modal_options: ModalOptions | None = None,
    ) -> BaseSandbox:
        """Construct a sandbox configured for CUDA execution."""
        bind_mounts = bind_mounts or []
        passthrough_paths = passthrough_paths or []
        extra_env = extra_env or {}
        extra_init_commands = extra_init_commands or []
        setup_fns = setup_fns or []

        # Pick a GPU lazily on first sandbox creation (modal manages its own).
        if kind is not SandboxKind.MODAL and self.selected_device is None:
            self.selected_device = self._pick_device()

        env = self._build_env(extra_env, kind)

        if kind is SandboxKind.LOCAL:
            # LocalShellBackend (deepagents) has no setup_fns concept; for the
            # local sandbox there's nothing to install post-start anyway (no
            # docker symlinks, no restart scenarios), so we drop them silently.
            sandbox = LocalShellBackend(
                root_dir=host_workspace,
                virtual_mode=True,
                inherit_env=True,
                env=env,
            )
        elif kind is SandboxKind.DOCKER:
            sandbox = DockerSandbox(
                host_workspace=host_workspace,
                image=self.image,
                gpus=self._docker_gpu_spec(),
                bind_mounts=bind_mounts,
                passthrough_paths=passthrough_paths,
                env=env,
                log_path=log_path,
                extra_init_commands=extra_init_commands,
                setup_fns=setup_fns,
            )
        elif kind is SandboxKind.MODAL:
            if modal_options is None:
                raise ValueError("modal_options is required for SandboxKind.MODAL")
            sandbox = ModalSandbox(
                host_workspace=host_workspace,
                image=self.image,
                gpu=modal_options.gpu,
                sandbox_timeout=modal_options.sandbox_timeout,
                idle_timeout=modal_options.idle_timeout,
                bind_mounts=bind_mounts,
                passthrough_paths=passthrough_paths,
                env=env,
                model_volume_name=modal_options.model_volume_name,
                extra_readonly_volumes=modal_options.extra_readonly_volumes,
                extra_writable_volumes=modal_options.extra_writable_volumes,
                log_path=log_path,
                extra_init_commands=extra_init_commands,
                setup_fns=setup_fns,
                app_name=modal_options.app_name,
            )
        else:
            raise ValueError(f"Unknown sandbox kind: {kind!r}")

        self._sandboxes.append((kind, sandbox))
        return sandbox

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        if self.selected_device is None:
            return None
        self._monitor = GpuContentionMonitor(
            log_dir=log_dir,
            gpu_uuid=self.selected_device.uuid,
        )
        return self._monitor

    def reselect_device(self) -> None:
        """Re-pick the least-loaded GPU; restart any docker sandboxes affected.

        Each restarted sandbox re-runs its ``setup_fns`` automatically as
        part of ``start()`` — callers don't need to replay anything.
        """
        if os.environ.get("CUDA_VISIBLE_DEVICES"):
            return  # user pinned GPU — respect it

        new_gpu = pick_gpu()
        if new_gpu is None:
            return
        if self.selected_device and new_gpu.index == self.selected_device.index:
            return

        old_index = self.selected_device.index if self.selected_device else None
        self.selected_device = new_gpu
        self._lprint(
            f"[gpu] Re-selected GPU {new_gpu.index} ({new_gpu.name}): "
            f"{new_gpu.memory_free_mib} MiB free (was GPU {old_index})"
        )
        self._save_gpu_metadata(new_gpu)

        for kind, sb in self._sandboxes:
            if kind is SandboxKind.DOCKER:
                sb.stop()
                sb._gpus = self._docker_gpu_spec()
                sb.start()  # re-runs setup_fns
            elif kind is SandboxKind.LOCAL:
                env = getattr(sb, "_env", None)
                if env is None:
                    env = {}
                    sb._env = env
                env["CUDA_VISIBLE_DEVICES"] = str(new_gpu.index)
            # SandboxKind.MODAL: remote GPU, nothing to restart.

        # Restart the contention monitor on the new device.
        if self._monitor is not None:
            self._monitor.stop()
        self._monitor = GpuContentionMonitor(
            log_dir=self.log_dir,
            gpu_uuid=new_gpu.uuid,
        )
        self._monitor.start()

    # -- internal ----------------------------------------------------------

    def _pick_device(self) -> GpuInfo | None:
        cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        if cuda_visible:
            self._lprint(f"[gpu] CUDA_VISIBLE_DEVICES={cuda_visible} set — skipping auto-selection")
            return None
        gpu = pick_gpu()
        if gpu is None:
            self._lprint("[gpu] No GPUs detected — skipping GPU selection")
            return None
        self._lprint(
            f"[gpu] Selected GPU {gpu.index} ({gpu.name}): "
            f"{gpu.memory_free_mib} MiB free / {gpu.memory_total_mib} MiB total, "
            f"{gpu.utilization_pct}% utilisation"
        )
        self._save_gpu_metadata(gpu)
        return gpu

    def _build_env(self, extra: dict[str, str], kind: SandboxKind) -> dict[str, str]:
        """Build env vars to set inside the sandbox.

        Composition order (last write wins):
          1. PyTorch wheel index — skipped for modal (remote driver is
             independent of host).
          2. ``CUDA_VISIBLE_DEVICES`` — the physical index for local sandboxes,
             but ``"0"`` for docker (where ``--gpus device=N`` already exposes
             the chosen GPU as device 0 inside the container).
          3. Caller's extras.
        """
        env: dict[str, str] = {}
        if kind is not SandboxKind.MODAL:
            env.update(self._pytorch_index_env())
        if self.selected_device is not None:
            if kind is SandboxKind.DOCKER:
                env["CUDA_VISIBLE_DEVICES"] = "0"
            else:
                env["CUDA_VISIBLE_DEVICES"] = str(self.selected_device.index)
        env.update(extra)
        return env

    def _docker_gpu_spec(self) -> str:
        """Translate the selected device into a Docker --gpus spec."""
        if self.selected_device is None:
            return "all"
        return f"device={self.selected_device.index}"

    @staticmethod
    def _pytorch_index_env() -> dict[str, str]:
        """Return ``UV_EXTRA_INDEX_URL`` matched to the host's CUDA driver.

        ``uv add torch`` will pick a wheel from this index instead of the
        default PyPI one (which may target a newer CUDA than the driver).
        Empty dict if nvidia-smi is missing or the driver version is unknown.
        """
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}

        driver_ver = result.stdout.strip().split("\n")[0]
        major = int(driver_ver.split(".")[0])
        cuda_tag_by_driver = {
            570: "cu128",
            565: "cu126",
            560: "cu124",
            555: "cu124",
            550: "cu121",
            545: "cu121",
            535: "cu121",
            525: "cu118",
        }
        cuda_tag = cuda_tag_by_driver.get(major)
        if cuda_tag is None:
            return {}
        return {"UV_EXTRA_INDEX_URL": f"https://download.pytorch.org/whl/{cuda_tag}"}

    def _save_gpu_metadata(self, gpu: GpuInfo) -> None:
        """Write GPU selection info to ``log_dir/gpu.json``."""
        all_gpus = query_gpu_info()
        data = {
            "selected_gpu": _gpu_to_dict(gpu),
            "all_gpus_at_selection": [_gpu_to_dict(g) for g in all_gpus],
            "selected_at": datetime.now().isoformat(),
            "contention_detected": False,
            "contention_events": 0,
        }
        (self.log_dir / "gpu.json").write_text(json.dumps(data, indent=2))


def _gpu_to_dict(g: GpuInfo) -> dict:
    return {
        "index": g.index,
        "uuid": g.uuid,
        "name": g.name,
        "memory_used_mib": g.memory_used_mib,
        "memory_total_mib": g.memory_total_mib,
        "memory_free_mib": g.memory_free_mib,
        "utilization_pct": g.utilization_pct,
    }
