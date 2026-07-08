"""Trainium backend: AWS NeuronCores + Neuron DLC container + neuron-explorer.

Trainium (Trn1/Trn2) is neither CUDA nor Metal:

* The accelerator is exposed to userspace as ``/dev/neuron*`` character
  devices, forwarded into the container with ``docker --device`` rather
  than the NVIDIA-only ``--gpus`` flag.
* The runtime/compiler ship in the AWS Neuron Deep Learning Container
  (``public.ecr.aws/neuron/pytorch-inference-neuronx``), which carries
  ``torch-neuronx`` / ``torch_xla`` / ``neuronx-cc`` so the agent can
  write explicit layers and compile them for NeuronCores.
* Profiling uses ``neuron-explorer`` (NEFF/NTFF), surfaced through the
  ``neuron`` profiler kind — nsys does not apply.

There is no per-device auto-selection: a trn instance exposes a fixed set
of ``/dev/neuron*`` devices and the Neuron runtime picks cores via
``NEURON_RT_VISIBLE_CORES`` if the implementer wants to pin them.  We
forward every device the host exposes and leave core selection to the
workload, so ``selected_device`` stays ``None`` and ``reselect_device``
is a no-op (parity with :class:`LocalBackend`).
"""

from __future__ import annotations

import glob
from collections.abc import Callable
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.sandbox import BaseSandbox

from vibe_serve.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    SetupFn,
)
from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind
from vibe_serve.sandbox.docker_sandbox import DockerSandbox

# AWS Neuron DLC.  Tag chosen to match the host's Neuron tools (2.30):
# PyTorch 2.9 / Python 3.12 / Neuron SDK 2.30 on Ubuntu 24.04.  Carries
# torch-neuronx, torch_xla, and the neuronx-cc compiler.  Override with
# ``--docker-image`` if the host SDK differs.
_DEFAULT_IMAGE = (
    "public.ecr.aws/neuron/pytorch-inference-neuronx:2.9.0-neuronx-py312-sdk2.30.0-ubuntu24.04"
)

# Docker's default /dev/shm is 64 MB; neuronx-cc and the Neuron runtime use
# shared memory and exhaust it ("No space left on device") when compiling a
# real model across shape buckets. trn2 instances have ample RAM, so give the
# container a generous shm.
_DEFAULT_SHM_SIZE = "16g"

# Persistent neuronx-cc compile cache, bind-mounted from the host so
# compiles (minutes each) survive container restarts and carry across
# rounds.  Kept *outside* /workspace so the git-tracked workspace doesn't
# balloon with multi-MB NEFFs.
_CACHE_CONTAINER_PATH = "/opt/neuron-compile-cache"
# neuronx-cc temp/workdir (TMPDIR), host-mounted so intermediates stay off the
# container overlay.
_TMP_CONTAINER_PATH = "/opt/neuron-tmp"


def _discover_neuron_devices() -> list[str]:
    """Return the host's ``/dev/neuron*`` character devices, sorted.

    Matches the numbered device nodes (``/dev/neuron0`` …) and skips the
    control node ``/dev/neuron_*`` if present.
    """
    devs = [d for d in glob.glob("/dev/neuron*") if d[len("/dev/neuron") :].isdigit()]
    return sorted(devs)


class TrainiumBackend:
    """AWS Trainium / NeuronCore backend (local or Docker; no Modal)."""

    name = ComputeBackend.TRAINIUM
    profiler_kind = ProfilerKind.NEURON

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
        # No per-device selection; kept for protocol parity (read by
        # _RunContext for logging/pinning).
        self.selected_device = None
        self._devices = _discover_neuron_devices()
        if self._devices:
            self._lprint(
                f"[neuron] Forwarding {len(self._devices)} device(s): {', '.join(self._devices)}"
            )
        else:
            self._lprint(
                "[neuron] No /dev/neuron* devices found on host — the "
                "container will start without an accelerator."
            )

    # -- ComputeBackendImpl protocol ---------------------------------------

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
        bind_mounts = list(bind_mounts or [])
        passthrough_paths = list(passthrough_paths or [])
        extra_env = dict(extra_env or {})
        extra_init_commands = list(extra_init_commands or [])
        setup_fns = setup_fns or []

        if kind is SandboxKind.MODAL:
            raise ValueError(
                "trainium backend does not support Modal — Modal offers no "
                "Trainium hardware. Use --docker (NeuronCores via "
                "/dev/neuron*) or local execution."
            )

        env = self._build_env(extra_env)

        if kind is SandboxKind.LOCAL:
            return LocalShellBackend(
                root_dir=host_workspace,
                virtual_mode=True,
                inherit_env=True,
                env=env,
            )

        if kind is SandboxKind.DOCKER:
            # Persistent host-side compile cache → container, kept out of
            # /workspace (and registered as passthrough so virtual-path
            # translation leaves its absolute path alone).
            host_cache = self.log_dir / "neuron-compile-cache"
            host_cache.mkdir(parents=True, exist_ok=True)
            bind_mounts.append((str(host_cache), _CACHE_CONTAINER_PATH, False))
            passthrough_paths.append(_CACHE_CONTAINER_PATH)

            # neuronx-cc writes large intermediates to its temp/workdir (TMPDIR).
            # Left on the container overlay these grow many GB per round and are
            # only reclaimed when the container is removed.  Redirect them to a
            # host-mounted dir so they live on the roomy host disk instead.
            host_tmp = self.log_dir / "neuron-tmp"
            host_tmp.mkdir(parents=True, exist_ok=True)
            bind_mounts.append((str(host_tmp), _TMP_CONTAINER_PATH, False))
            passthrough_paths.append(_TMP_CONTAINER_PATH)

            return DockerSandbox(
                host_workspace=host_workspace,
                image=self.image,
                gpus=None,  # Neuron uses --device, not --gpus
                devices=self._devices,
                # The Neuron DLC's ENTRYPOINT launches a model server; clear it
                # so the sandbox container idles on `sleep infinity` and we can
                # exec agent commands into it.
                entrypoint="",
                # neuronx-cc needs far more than Docker's default 64 MB /dev/shm.
                shm_size=_DEFAULT_SHM_SIZE,
                # Reclaim the container's overlay (GBs of compiled artifacts)
                # automatically when it goes away, even on a hard kill.
                auto_remove=True,
                bind_mounts=bind_mounts,
                passthrough_paths=passthrough_paths,
                env=env,
                log_path=log_path,
                extra_init_commands=extra_init_commands,
                setup_fns=setup_fns,
            )

        raise ValueError(f"Unknown sandbox kind: {kind!r}")

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        # neuron-monitor exists, but shared-device contention handling
        # isn't wired up yet; skip rather than fake it.
        return None

    def reselect_device(self) -> None:
        return None

    # -- internal ----------------------------------------------------------

    def _build_env(self, extra: dict[str, str]) -> dict[str, str]:
        """Neuron runtime env, with caller extras taking precedence."""
        env: dict[str, str] = {
            # Persistent neuronx-cc cache so repeated compiles are cheap.
            "NEURON_COMPILE_CACHE_URL": _CACHE_CONTAINER_PATH,
            # Keep neuronx-cc's large temp/workdir on the host mount, not the
            # container overlay.
            "TMPDIR": _TMP_CONTAINER_PATH,
        }
        env.update(extra)
        return env
