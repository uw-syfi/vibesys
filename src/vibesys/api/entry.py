"""Config loading for run requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.config import load_config as _load_config

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.contracts import Config


def load_config(path: Path, *, ignored_sections: frozenset[str] = frozenset()) -> Config:
    """Load and validate core configuration from a shared TOML file."""
    return _load_config(path, ignored_sections=ignored_sections)
