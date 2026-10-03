"""Database domain definition.

Hosts in-place optimization of real database / dataflow engines.
"""

from __future__ import annotations

from pathlib import Path

from vibesys.constants import DomainName
from vibesys.domains.base import DomainDefinition

DEFINITION = DomainDefinition(
    name=DomainName.DATABASE,
    prompt_dir=Path(__file__).resolve().parent / "prompts",
)
