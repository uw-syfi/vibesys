"""Shared VibeSys paths and constant values."""

from enum import StrEnum
from pathlib import Path

from vs_sandbox.api import ComputeBackend

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = ".agents/skills/"


class DomainName(StrEnum):
    """Known optimization domains with framework-owned integrations."""

    LLM_SERVING = "llm-serving"
    GENERIC = "generic"
    MICROSERVICES = "microservices"
    DATABASE = "database"
    KERNEL_WRITING = "kernel-writing"


DEFAULT_COMPUTE_BACKEND = ComputeBackend.CUDA
KNOWN_COMPUTE_BACKENDS: tuple[str, ...] = tuple(b.value for b in ComputeBackend)
