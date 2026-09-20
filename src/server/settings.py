"""Configuration contracts exposed to frontend clients."""

from __future__ import annotations

import tomllib
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import RepositoryVisibility

if TYPE_CHECKING:
    from pathlib import Path


class TuiTheme(StrEnum):
    """Selectable terminal UI themes."""

    DARK = "dark"
    LIGHT = "light"
    SOLARIZED_DARK = "solarized-dark"
    SOLARIZED_LIGHT = "solarized-light"
    CATPPUCCIN_MOCHA = "catppuccin-mocha"
    CATPPUCCIN_LATTE = "catppuccin-latte"
    HIGH_CONTRAST_DARK = "high-contrast-dark"
    HIGH_CONTRAST_LIGHT = "high-contrast-light"


DEFAULT_TUI_THEME = TuiTheme.DARK
KNOWN_TUI_THEMES: tuple[str, ...] = tuple(theme.value for theme in TuiTheme)


class _TuiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: TuiTheme = Field(default=DEFAULT_TUI_THEME)


def load_tui_theme(path: Path | None) -> TuiTheme:
    """Load only ``[tui].theme`` from a shared configuration file.

    Other top-level sections belong to the core configuration loader and are
    ignored here. The TUI table itself is strict so misspelled presentation
    settings fail at the application boundary.
    """
    if path is None:
        return DEFAULT_TUI_THEME
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    table = document.get("tui")
    if table is None:
        return DEFAULT_TUI_THEME
    return _TuiSettings.model_validate(table).theme


class InteractiveSetupDefaults(BaseModel):
    """JSON contract passed to the interactive launch form."""

    model_config = ConfigDict(extra="forbid")

    runs_dir: str
    input_path: str
    experiment_name: str
    repository_owner: str | None
    repository_name: str
    visibility: RepositoryVisibility
    theme: TuiTheme
