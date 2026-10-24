"""Trainium backend: AWS NeuronCores and a Neuron DLC container.

Trainium (Trn1/Trn2) is neither CUDA nor Metal:

* The accelerator is exposed to userspace as ``/dev/neuron*`` character
  devices, forwarded into the container with ``docker --device`` rather
  than the NVIDIA-only ``--gpus`` flag.
* The runtime/compiler ship in the AWS TorchNeuron Deep Learning Container
  (``public.ecr.aws/neuron/torch-neuronx``), which carries native
  TorchNeuron (``torch-neuronx`` 2.14: the ``neuron`` PyTorch device, eager
  mode, and ``torch.compile(backend="neuron")``), ``nki`` and ``neuronx-cc``
  so the agent can write explicit layers and compile them for NeuronCores.
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

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.compute_backends import (
    ComputeBackend,
    ContentionMonitor,
    SandboxKind,
    make_local_shell_sandbox,
)
from vs_sandbox.container_memory import ContainerMemoryCap

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sandbox.accelerator_discovery import AcceleratorDiscovery
    from vs_sandbox.docker_cli import DockerCli
    from vs_sandbox.execution import CommandRunner
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks
    from vs_sim.api import Clock, PipedProcessLauncher, Threads

# AWS TorchNeuron DLC: native PyTorch 2.14 / Python 3.14 / Neuron SDK 2.33 on
# Ubuntu 26.04. Carries torch-neuronx (native backend, not the retired
# torch_xla lineage), nki, neuronx-cc and neuron-explorer. Override with
# ``--docker-image`` if the host needs a different SDK. The image installs
# TorchNeuron in editable mode from /workspace, which the project mount hides;
# trainium tasks carry a task Dockerfile that relocates it (see the
# Llama-3-8B-trn2 example).
DEFAULT_IMAGE = "public.ecr.aws/neuron/torch-neuronx:2.14.0-neuronx-py314-sdk2.33.0-ubuntu26.04"

# Docker's default /dev/shm is 64 MB; neuronx-cc and the Neuron runtime use
# shared memory and exhaust it ("No space left on device") when compiling a
# real model across shape buckets. trn2 instances have ample RAM, so give the
# container a generous shm.
_DEFAULT_SHM_SIZE = "16g"

# Persistent TorchNeuron compile caches (NEFFs, lowered HLO, NKI kernels),
# bind-mounted from the host so compiles (minutes each) survive container
# restarts and carry across rounds. Kept outside the container's /workspace
# project mount so the Git-tracked project does not balloon with multi-MB NEFFs.
_CACHE_CONTAINER_PATH = "/opt/neuron-compile-cache"
# neuronx-cc temp/workdir (TMPDIR), host-mounted so intermediates stay off the
# container overlay.
_TMP_CONTAINER_PATH = "/opt/neuron-tmp"


class TrainiumBackend:
    """AWS Trainium / NeuronCore backend (local or Docker; no Modal)."""

    name = ComputeBackend.TRAINIUM

    # lint-waiver: LW-731408 [PLR0913]; the registry's shared construction keywords plus discovery.
    # > Grouping them would only rename the keywords create_compute_backend passes every
    # > registered backend; dropping clock would make the registry branch on a backend.
    def __init__(  # noqa: PLR0913
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        accelerator_discovery: AcceleratorDiscovery,
        docker: DockerCli,
        threads: Threads,
        launcher: PipedProcessLauncher,
        clock: Clock,
        events: object | None = None,
    ) -> None:
        """Configure Trainium execution with its log directory and image override."""
        del clock  # nothing this backend records carries a timestamp
        del events  # Trainium contention monitoring is not implemented yet
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image or DEFAULT_IMAGE
        self._docker = docker
        self._memory = ContainerMemoryCap(docker)
        self._threads = threads
        self._launcher = launcher
        # No per-device selection; kept for protocol parity (read by
        # run resources for logging/pinning).
        self.selected_device = None
        discovery = accelerator_discovery
        self._devices = discovery.discover_trainium().device_nodes
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

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-011114 [PLR0913]; RunEnvironment dispatches this structural ComputeBackendImpl method with shared named sandbox options; changing it would break backend parity.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        extra_env: dict[str, str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
        docker_in_docker: bool = False,
        same_path_workspace: bool = False,
        run_id: str | None = None,
    ) -> CommandRunner:
        """Create a local or Trainium-enabled Docker sandbox."""
        # Deferred: importing DockerSandbox registers process-wide signal and
        # atexit handlers. Registration must stay side-effect free.
        docker_sandbox = import_module("vs_sandbox.api").DockerSandbox

        bind_mounts = list(bind_mounts or [])
        extra_env = dict(extra_env or {})
        lifecycle_hooks = lifecycle_hooks or []
        del ephemeral

        env = self._build_env(extra_env)

        if kind is SandboxKind.LOCAL:
            # ``_build_env`` names the *container* cache and temp paths, which
            # only exist because the Docker branch bind-mounts them. A local run
            # has no such mounts: /opt is root-owned and read-only inside the
            # agent sandbox, so neuronx-cc would fail to write either one.
            # Redirect both under the run's log directory, which is writable.
            host_cache = self.log_dir / "neuron-compile-cache"
            host_tmp = self.log_dir / "neuron-tmp"
            host_cache.mkdir(parents=True, exist_ok=True)
            host_tmp.mkdir(parents=True, exist_ok=True)
            local_env = {**env, **_cache_env(cache_root=host_cache, tmp_root=host_tmp)}
            local_env.update(extra_env)
            return make_local_shell_sandbox(
                host_workspace=host_workspace,
                env=local_env,
                lifecycle_hooks=lifecycle_hooks,
                threads=self._threads,
                launcher=self._launcher,
            )

        if kind is SandboxKind.DOCKER:
            # Persistent host-side compile cache → container, kept out of
            # the container's /workspace project mount.
            host_cache = self.log_dir / "neuron-compile-cache"
            host_cache.mkdir(parents=True, exist_ok=True)
            bind_mounts.append((str(host_cache), _CACHE_CONTAINER_PATH, False))

            # neuronx-cc writes large intermediates to its temp/workdir (TMPDIR).
            # Left on the container overlay these grow many GB per round and are
            # only reclaimed when the container is removed.  Redirect them to a
            # host-mounted dir so they live on the roomy host disk instead.
            host_tmp = self.log_dir / "neuron-tmp"
            host_tmp.mkdir(parents=True, exist_ok=True)
            bind_mounts.append((str(host_tmp), _TMP_CONTAINER_PATH, False))

            return docker_sandbox(
                # A runaway process dies inside its container, not the host.
                memory_limit=self._memory.limit(),
                host_workspace=host_workspace,
                image=container_image or self.image,
                gpus=None,  # Neuron uses --device, not --gpus
                devices=list(self._devices) if attach_accelerator else [],
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
                resources=resources,
                env=env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
                docker=self._docker,
                threads=self._threads,
                docker_in_docker=docker_in_docker,
                same_path_workspace=same_path_workspace,
                run_id=run_id,
            )

        message = f"Unknown sandbox kind: {kind!r}"
        raise ValueError(message)

    def ensure_accelerator_available(self) -> None:
        """No-op: a missing ``/dev/neuron*`` is already logged at construction, not raised.

        Unlike CUDA's late ``docker run --gpus`` failure, a Trainium sandbox without
        device nodes still starts (CPU-only); see the constructor's warning.
        """
        return

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Return no monitor until Trainium contention handling is available."""
        del log_dir
        # neuron-monitor exists, but shared-device contention handling
        # isn't wired up yet; skip rather than fake it.
        return None

    def reselect_device(self) -> None:
        """Do nothing because Trainium device reselection is not implemented."""
        return

    # -- internal ----------------------------------------------------------

    def _build_env(self, extra: dict[str, str]) -> dict[str, str]:
        """Neuron runtime env, with caller extras taking precedence."""
        env = _cache_env(cache_root=Path(_CACHE_CONTAINER_PATH), tmp_root=Path(_TMP_CONTAINER_PATH))
        env.update(extra)
        return env


def _cache_env(*, cache_root: Path, tmp_root: Path) -> dict[str, str]:
    """TorchNeuron cache and temp locations under the persistent cache and temp roots.

    Every TorchNeuron cache defaults to ``/tmp``, which dies with the container,
    so each is pointed under ``cache_root``. File locks stay under ``tmp_root``:
    TorchNeuron requires a local POSIX filesystem for them.
    """
    return {
        # Compiled NEFFs, keyed by lowered HLO + compiler config + version.
        "TORCH_NEURONX_NEFF_CACHE_DIR": str(cache_root / "neff"),
        # Lowered StableHLO, shared by eager mode and torch.compile.
        "TORCH_NEURONX_HLO_CACHE_DIR": str(cache_root / "hlo"),
        # Compiled NKI kernels.
        "NKI_TRACE_CACHE_URL": str(cache_root / "nki"),
        "TORCH_NEURONX_NEFF_LOCAL_CACHE_DIR": str(tmp_root / "neff-locks"),
        # Keep neuronx-cc's large temp/workdir on the host mount, not the
        # container overlay.
        "TMPDIR": str(tmp_root),
    }
