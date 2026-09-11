from enum import StrEnum  # noqa: D100  # tracked: #288
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = ".agents/skills/"

# ANSI colors
DIM = "\033[2m"
RED = "\033[31m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"

ANTHROPIC_PREFIXES = ("claude-",)
GOOGLE_PREFIXES = ("gemini-", "gemma-")
OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4")


class ComputeBackend(StrEnum):
    """Compute backends the agent can target.

    Add a new variant here when a compute stack (sandbox image,
    GPU/device selection, profiler, and prompt/runtime support) is wired up
    end-to-end.

    - ``CUDA`` is fully supported: NVIDIA container, nvidia-smi GPU
      selection, nsys profiler, FlashInfer-style optimizations.
    - ``METAL`` (Apple Silicon) is local-only — Docker can't reach Apple
      GPUs (nor can a remote GPU provider), so ``LocalBackend.make_sandbox``
      raises on anything other than ``SandboxKind.LOCAL``. The serving-domain
      templates remain CUDA-flavoured (FlashInfer, CUDA graphs, nsys), so use
      a target whose prompts and tools support Metal.
    - ``TRAINIUM`` (AWS Trn1/Trn2) targets NeuronCores via an AWS Neuron
      DLC container. The host's ``/dev/neuron*`` devices are passed
      through to the container (``--device``, *not* ``--gpus``);
      profiling uses ``neuron-explorer`` instead of nsys.
      ``TrainiumBackend.make_sandbox`` supports only ``SandboxKind.LOCAL``
      and ``SandboxKind.DOCKER``.
    - ``ROCM`` (AMD Instinct) targets CDNA GPUs via a ROCm PyTorch
      container.  The host's ``/dev/kfd`` and ``/dev/dri/*`` nodes are
      passed through (``--device`` + ``--group-add``, *not* ``--gpus``);
      profiling reuses ``torch`` since ``torch.profiler`` works on ROCm.
      ``RocmBackend.make_sandbox`` supports only ``SandboxKind.LOCAL`` and
      ``SandboxKind.DOCKER``.  **Experimental**: wired end to end but not
      yet exercised against MI300-class hardware, and serving-domain prompts
      may require target-specific adaptation.
    - ``CPU`` has no GPU at all: device selection and the hardware monitor are
      no-ops. It supports local execution and CPU-only Docker containers. It
      targets CPU-bound workloads (KV stores, networking servers) where the
      win is in the code, not the kernels.
    """

    CUDA = "cuda"
    METAL = "metal"
    TRAINIUM = "trainium"
    ROCM = "rocm"
    CPU = "cpu"


DEFAULT_COMPUTE_BACKEND = ComputeBackend.CUDA
KNOWN_COMPUTE_BACKENDS: tuple[str, ...] = tuple(b.value for b in ComputeBackend)

# Agent backend used when neither the ``--agent-backend`` flag nor an
# ``[agent].backend`` config key is set. Resolved in a single place so
# build_agent_client and ComputeContext cannot drift.
DEFAULT_AGENT_BACKEND = "cli"
