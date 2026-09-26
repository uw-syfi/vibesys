"""Every registered strategy has a prompt folder, and vice versa.

A strategy is its folder + one registry line + its prompt folder
(``prompts/loops/<strategy>/``). This test derives "which folder backs a
registered strategy" the same way ``vibesys.loops.registry`` does: each
registration's orchestrator class lives in ``vibesys.loops.<folder>...``, so
its ``__module__`` names the folder.
"""

from __future__ import annotations

from pathlib import Path

from vibesys.loops.registry import built_in_orchestrations
from vibesys.prompts import PROMPTS_DIR

_PROMPTS_LOOPS = Path(PROMPTS_DIR) / "loops"


def _registered_strategy_folders() -> set[str]:
    registry = built_in_orchestrations()
    folders = set()
    for registration in registry._registrations.values():  # noqa: SLF001  # LW-040196 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        module = registration.orchestrator.__module__
        assert module.startswith("vibesys.loops."), (
            f"registered orchestrator {module!r} is not under vibesys.loops"
        )
        folders.add(module.split(".")[2])
    return folders


def test_every_registered_strategy_has_a_prompt_folder() -> None:
    folders = _registered_strategy_folders()
    missing = sorted(folder for folder in folders if not (_PROMPTS_LOOPS / folder).is_dir())
    assert not missing, f"registered strategies with no prompts/loops/<strategy>: {missing}"


def test_every_prompt_loop_folder_maps_to_a_registered_strategy() -> None:
    folders = _registered_strategy_folders()
    orphaned = sorted(
        path.name for path in _PROMPTS_LOOPS.iterdir() if path.is_dir() and path.name not in folders
    )
    assert not orphaned, f"prompts/loops/ folders with no registered strategy: {orphaned}"
