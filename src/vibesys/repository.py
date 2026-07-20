"""Repository configuration contracts and experiment-name generation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class RepositoryVisibility(StrEnum):
    """Supported GitHub repository visibility values."""

    PRIVATE = "private"
    PUBLIC = "public"
    INTERNAL = "internal"


REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class InteractiveSetupDefaults(BaseModel):
    """JSON contract passed from Python configuration to the pre-launch TUI."""

    model_config = ConfigDict(extra="forbid")

    input_path: str
    experiment_name: str
    repository_owner: str | None
    repository_name: str
    visibility: RepositoryVisibility


def generate_experiment_name(
    input_path: Path | None,
    *,
    now: datetime | None = None,
) -> str:
    """Generate a readable, collision-resistant name for an interactive run."""
    source_name = input_path.name if input_path is not None else "experiment"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_name).strip("-._").lower()
    if not base:
        base = "experiment"
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{base}-{timestamp}"


def repository_name_from_experiment(experiment_name: str) -> str:
    """Convert an experiment label into a valid GitHub repository component."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", experiment_name).strip("-._").lower()
    return name or "experiment"
