"""Shared VibeSys paths and constant values."""

from enum import StrEnum
from pathlib import Path

from vs_sandbox.api import ComputeBackend

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


class DomainName(StrEnum):
    """Known optimization domains with framework-owned integrations."""

    LLM_SERVING = "llm-serving"
    GENERIC = "generic"
    MICROSERVICES = "microservices"
    DATABASE = "database"


DEFAULT_COMPUTE_BACKEND = ComputeBackend.CUDA
KNOWN_COMPUTE_BACKENDS: tuple[str, ...] = tuple(b.value for b in ComputeBackend)
