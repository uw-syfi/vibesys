"""Every registered plugin owns its prompt code and resources.

Plugin declarations live in ``vibesys.orchestrations.<folder>``. A plugin may
use a ``prompts.py`` module or a ``prompts/`` package; neither requires a
duplicate central ``prompts/loops/<strategy>/`` directory.
"""

from __future__ import annotations

from pathlib import Path

from vibesys.plugin_catalog import built_in_orchestrations
from vibesys.prompts import PROMPTS_DIR

_PROMPTS_LOOPS = Path(PROMPTS_DIR) / "loops"


def _registered_strategy_folders() -> set[str]:
    registry = built_in_orchestrations()
    folders = set()
    for registration in registry._registrations.values():  # noqa: SLF001  # LW-040196 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        module = registration.plugin.orchestrate.__module__
        prefix = "vibesys.orchestrations."
        assert module.startswith(prefix), f"registered policy {module!r} is not under {prefix}"
        folders.add(module.split(".")[2])
    return folders


def test_every_registered_strategy_has_a_local_prompt_owner() -> None:
    folders = _registered_strategy_folders()
    orchestrations = Path(__file__).parents[3] / "src" / "vibesys" / "orchestrations"
    missing = sorted(
        folder
        for folder in folders
        if not (orchestrations / folder / "prompts.py").is_file()
        and not (orchestrations / folder / "prompts").is_dir()
    )
    assert not missing, f"registered strategies with no plugin-local prompt owner: {missing}"


def test_every_remaining_legacy_prompt_folder_maps_to_a_registered_strategy() -> None:
    folders = _registered_strategy_folders()
    orphaned = sorted(
        path.name for path in _PROMPTS_LOOPS.iterdir() if path.is_dir() and path.name not in folders
    )
    assert not orphaned, f"prompts/loops/ folders with no registered strategy: {orphaned}"
