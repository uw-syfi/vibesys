from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QueueInputConfig:
    scenario: str | None = None
    capacity: int | None = None
    producers: int | None = None
    consumers: int | None = None


def load_config(project_dir: Path | None = None) -> QueueInputConfig:
    pyproject = (project_dir or Path.cwd()) / "pyproject.toml"
    if not pyproject.is_file():
        return QueueInputConfig()

    data = tomllib.loads(pyproject.read_text())
    queue = data.get("tool", {}).get("vibeserve", {}).get("queue", {})
    if not isinstance(queue, dict):
        return QueueInputConfig()

    return QueueInputConfig(
        scenario=_optional_str(queue.get("scenario")),
        capacity=_optional_int(queue.get("capacity")),
        producers=_optional_int(queue.get("producers")),
        consumers=_optional_int(queue.get("consumers")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
