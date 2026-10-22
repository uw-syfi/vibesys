"""Config loading for run requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.config import (
    load_config as _load_config,
)
from vibesys.config import (
    load_host_allowed_models as _load_host_allowed_models,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.contracts import Config


def load_config(path: Path, *, ignored_sections: frozenset[str] = frozenset()) -> Config:
    """Load and validate core configuration from a shared TOML file."""
    return _load_config(path, ignored_sections=ignored_sections)


def load_host_allowed_models(path: Path | None = None) -> frozenset[str]:
    """The models the host file allows, or none when that file is missing."""
    return _load_host_allowed_models(path)
