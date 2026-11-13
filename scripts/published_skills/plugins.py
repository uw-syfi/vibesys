"""Which skill folders each published plugin lists.

Every plugin keeps the one source root ``./skills`` and differs only by its explicit skill
list, so no skill file moves and one skill may be listed by several plugins. A skill belongs to
a plugin when its folder equals a prefix or lies below it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

PLUGIN_SKILLS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "vibesys": ("./search",),
        "vibesys-llm-serving": ("./domains/serving-systems",),
        # Tokio is cross-cutting: dial9-tokio is listed here and in vibesys-database on purpose.
        "vibesys-microservices": (
            "./domains/microservice-optimization",
            "./domains/custom-rpc-optimization",
            "./domains/dial9-tokio",
        ),
        "vibesys-database": ("./domains/dial9-tokio",),
        # The backend-neutral kernel skills are listed in every kernels plugin on purpose:
        # one install per task backend gives everything the task needs.
        "vibesys-kernels-cuda": (
            "./domains/kernel-correctness",
            "./domains/kernel-performance",
            "./domains/kernel-ncu-analysis",
            "./domains/kernel-timeline-analysis",
        ),
        "vibesys-kernels-rocm": (
            "./domains/kernel-correctness",
            "./domains/kernel-performance",
            "./domains/kernel-rocprof-analysis",
        ),
        "vibesys-kernels-trainium": (
            "./domains/kernel-correctness",
            "./domains/kernel-performance",
            "./domains/neuron-agentic-development",
        ),
        "vibesys-cli-tools": ("./domains/cli-tool-optimization",),
    }
)
"""Plugin name to the skill-folder prefixes it lists, relative to ``skills/``."""
