from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = ".agents/skills/"

# ANSI colors
_DIM = "\033[2m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RESET = "\033[0m"

_ANTHROPIC_PREFIXES = ("claude-",)
_GOOGLE_PREFIXES = ("gemini-", "gemma-")
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4")

class ComputeBackend(StrEnum):
    """Compute backends the agent can target.

    Add a new variant here when a compute stack (sandbox image,
    GPU/device selection, profiler, curriculum) is wired up end-to-end.

    - ``CUDA`` is fully supported: NVIDIA container, nvidia-smi GPU
      selection, nsys profiler, FlashInfer-style optimizations.
    - ``METAL`` (Apple Silicon) is local-only — Docker/Modal sandboxes
      can't reach Apple GPUs, so ``MetalBackend.make_sandbox`` raises on
      anything other than ``SandboxKind.LOCAL``. The curriculum
      templates are still CUDA-flavoured (FlashInfer, CUDA graphs,
      nsys) so ``vibeserve-curriculum --backend metal`` is not a
      supported workflow yet; the simple loop is the intended entry
      point.
    """

    CUDA = "cuda"
    METAL = "metal"


DEFAULT_COMPUTE_BACKEND = ComputeBackend.CUDA
KNOWN_COMPUTE_BACKENDS: tuple[str, ...] = tuple(b.value for b in ComputeBackend)

# Agent backend used when neither the ``--agent-backend`` flag nor an
# ``[agent].backend`` config key is set. Resolved in a single place so
# build_agent_runner and ComputeContext cannot drift.
DEFAULT_AGENT_BACKEND = "cli"
