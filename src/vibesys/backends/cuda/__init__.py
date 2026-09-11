"""CUDA backend: NVIDIA GPU + nvcr.io PyTorch container + nsys profiler."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence  # noqa: TC003  # tracked: #288
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.backends.base import (
    ContentionMonitor,
    SandboxKind,
    make_local_shell_sandbox,
)
from vibesys.backends.cuda.gpu_monitor import (
    GpuContentionMonitor,
    GpuInfo,
    pick_gpu,
    query_gpu_info,
)
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind

if TYPE_CHECKING:
    # Annotation only; deepagents pulls langchain + anthropic (~seconds).
    from deepagents.backends.protocol import SandboxBackendProtocol

    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks

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
        self.selected_device: GpuInfo | None = None
        self._monitor: GpuContentionMonitor | None = None
        # Sandboxes built so far, so maybe_rebalance can find them without
        # the caller having to thread them back in.
        # (kind, sandbox) tuples — kind is recorded at registration time so
        # ``reselect_device`` dispatches on the requested kind rather than on
        # the concrete sandbox class.
        self._sandboxes: list[tuple[SandboxKind, SandboxBackendProtocol]] = []

    # -- ComputeBackendImpl protocol ---------------------------------------------

    def make_sandbox(  # noqa: PLR0913  # tracked: #288
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
        """Construct a sandbox configured for CUDA execution."""
        # Deferred: the sandbox classes subclass deepagents' BaseSandbox, which
        # pulls langchain + anthropic. Registration must stay import-cheap.
        from vs_sandbox import DockerSandbox  # noqa: PLC0415  # tracked: #288

        bind_mounts = bind_mounts or []
        passthrough_paths = passthrough_paths or []
        extra_env = extra_env or {}
        # Accepted for ComputeBackendImpl protocol parity but unused: neither
        # the LOCAL sandbox nor the agent-image-based DOCKER sandbox runs
        # per-launch install commands.
        del extra_init_commands
        lifecycle_hooks = lifecycle_hooks or []

        # Pick a GPU lazily on first sandbox creation.
        if attach_accelerator and self.selected_device is None:
            self.selected_device = self._pick_device()

        env = self._build_env(
            extra_env,
            kind,
            attach_accelerator=attach_accelerator,
        )

        if kind is SandboxKind.LOCAL:
            sandbox = make_local_shell_sandbox(
                host_workspace=host_workspace,
                env=env,
                lifecycle_hooks=lifecycle_hooks,
            )
        elif kind is SandboxKind.DOCKER:
            sandbox = DockerSandbox(
                host_workspace=host_workspace,
                image=container_image or self.image,
                gpus=self._docker_gpu_spec() if attach_accelerator else None,
                bind_mounts=bind_mounts,
                resources=resources,
                passthrough_paths=passthrough_paths,
                env=env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
            )
        else:
            raise ValueError(f"Unknown sandbox kind: {kind!r}")  # noqa: TRY003  # tracked: #288

        if not ephemeral:
            self._sandboxes.append((kind, sandbox))
        return sandbox

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:  # noqa: D102  # tracked: #288
        if self.selected_device is None:
            return None
        self._monitor = GpuContentionMonitor(
            log_dir=log_dir,
            gpu_uuid=self.selected_device.uuid,
        )
        return self._monitor

    def reselect_device(self) -> None:
        """Re-pick the least-loaded GPU; restart any docker sandboxes affected.

        Each restarted sandbox re-runs its lifecycle hooks automatically as
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

        # Deferred for the same reason as in make_sandbox; by the time a
        # rebalance happens both modules are already imported.
        from deepagents.backends import LocalShellBackend  # noqa: PLC0415  # tracked: #288

        from vs_sandbox import DockerSandbox  # noqa: PLC0415  # tracked: #288

        # Kind-dispatched pokes at sandbox internals: DOCKER entries are
        # always DockerSandbox (stop/start/_gpus), LOCAL entries are always
        # LocalShellBackend (_env). The recorded kind still selects the
        # branch; the assertions only state that registration invariant so
        # the concrete attributes resolve.
        for kind, sb in self._sandboxes:
            if kind is SandboxKind.DOCKER:
                assert isinstance(sb, DockerSandbox)  # noqa: S101  # registration invariant
                sb.stop()
                sb._gpus = self._docker_gpu_spec()  # noqa: SLF001  # tracked: #288
                sb.start()  # re-runs lifecycle hooks
            elif kind is SandboxKind.LOCAL:
                assert isinstance(sb, LocalShellBackend)  # noqa: S101  # registration invariant
                env: dict[str, str] | None = getattr(sb, "_env", None)
                if env is None:
                    env = {}
                    sb._env = env  # noqa: SLF001  # tracked: #288
                env["CUDA_VISIBLE_DEVICES"] = str(new_gpu.index)

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

    def _build_env(
        self,
        extra: dict[str, str],
        kind: SandboxKind,
        *,
        attach_accelerator: bool = True,
    ) -> dict[str, str]:
        """Build env vars to set inside the sandbox.

        Composition order (last write wins):
          1. PyTorch wheel index, matched to the host's CUDA driver.
          2. ``CUDA_VISIBLE_DEVICES`` — the physical index for local sandboxes,
             but ``"0"`` for docker (where ``--gpus device=N`` already exposes
             the chosen GPU as device 0 inside the container).
          3. Caller's extras.
        """
        env: dict[str, str] = self._pytorch_index_env()
        if attach_accelerator and self.selected_device is not None:
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
            result = subprocess.run(  # noqa: PLW1510  # tracked: #288
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],  # noqa: S607  # tracked: #288
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
            "selected_at": datetime.now().isoformat(),  # noqa: DTZ005  # tracked: #288
            "contention_detected": False,
            "contention_events": 0,
        }
        (self.log_dir / "gpu.json").write_text(json.dumps(data, indent=2))


def _gpu_to_dict(g: GpuInfo) -> dict[str, int | str]:
    return {
        "index": g.index,
        "uuid": g.uuid,
        "name": g.name,
        "memory_used_mib": g.memory_used_mib,
        "memory_total_mib": g.memory_total_mib,
        "memory_free_mib": g.memory_free_mib,
        "utilization_pct": g.utilization_pct,
    }
