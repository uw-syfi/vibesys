"""The theme name list is duplicated across Python and TypeScript; keep it honest.

``server.settings.TuiTheme`` validates ``--theme`` and ``[tui].theme``;
``clients/tui/src/theme.ts`` owns the color definitions and validates the
name again in the launcher. A name present in only one of them is either a
flag the client cannot render or a theme the backend rejects, so this test
pins the two lists together.
"""

from __future__ import annotations

import re

from server.settings import DEFAULT_TUI_THEME, KNOWN_TUI_THEMES, TuiTheme
from vibesys.constants import PROJECT_ROOT

_THEME_MODULE = PROJECT_ROOT / "clients" / "tui" / "src" / "theme.ts"
_THEME_NAMES_BLOCK = re.compile(r"export const THEME_NAMES = \[(.*?)\] as const;", re.DOTALL)
_DEFAULT_THEME = re.compile(r"export const DEFAULT_THEME_NAME: ThemeName = '([\w-]+)';")


def _typescript_theme_names() -> list[str]:
    match = _THEME_NAMES_BLOCK.search(_THEME_MODULE.read_text(encoding="utf-8"))
    assert match is not None, f"THEME_NAMES not found in {_THEME_MODULE}"
    return re.findall(r"'([\w-]+)'", match.group(1))


def test_python_and_typescript_agree_on_theme_names() -> None:
    assert _typescript_theme_names() == list(KNOWN_TUI_THEMES)


def test_python_and_typescript_agree_on_the_default_theme() -> None:
    match = _DEFAULT_THEME.search(_THEME_MODULE.read_text(encoding="utf-8"))
    assert match is not None, f"DEFAULT_THEME_NAME not found in {_THEME_MODULE}"
    assert match.group(1) == DEFAULT_TUI_THEME.value


def test_themes_are_declared_as_light_dark_pairs() -> None:
    dark = [theme for theme in TuiTheme if theme.value.endswith(("dark", "mocha"))]
    light = [theme for theme in TuiTheme if theme.value.endswith(("light", "latte"))]
    assert len(dark) == len(light) == 4
    assert len(dark) + len(light) == len(TuiTheme)


def test_dark_is_the_default_so_the_baseline_appearance_is_unchanged() -> None:
    assert DEFAULT_TUI_THEME is TuiTheme.DARK
