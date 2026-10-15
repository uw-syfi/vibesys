"""Every registered plugin owns its prompt code and resources.

Plugin declarations live in ``vibesys.orchestration.<folder>``. A plugin may
use a ``prompts.py`` module or a ``prompts/`` package; neither requires a
duplicate central ``prompts/loops/<strategy>/`` directory.
"""

from __future__ import annotations

from pathlib import Path

from launch import built_in_orchestrations

_ORCHESTRATION = Path(__file__).parents[3].resolve() / "src" / "vibesys" / "orchestration"


def _core_prompt_folders() -> set[Path]:
    """The prompt folders that core policies declare."""
    return {core for _, core, _ in _registrations()}


def _registrations() -> set[tuple[str, Path, bool]]:
    registry = built_in_orchestrations()
    found: set[tuple[str, Path, bool]] = set()
    for registration in registry._registrations.values():  # noqa: SLF001  # LW-040196 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        # A core policy's prompt folder is its prompt owner.
        location = Path(registration.plugin.core.prompt_templates).resolve()
        found.add(
            (
                location.relative_to(_ORCHESTRATION).parts[0],
                location,
                bool(registration.plugin.agents),
            )
        )
    return found


def test_every_registered_strategy_has_a_local_prompt_owner() -> None:
    orchestration = _ORCHESTRATION
    outside = sorted(
        str(folder) for folder in _core_prompt_folders() if orchestration not in folder.parents
    )
    assert not outside, f"core policies whose prompt folder is outside a strategy: {outside}"
    empty = sorted(
        str(folder.relative_to(orchestration))
        for _, folder, starts_agents in _registrations()
        if starts_agents and not any(folder.glob("*.j2"))
    )
    assert not empty, f"core policies whose prompt folder holds no templates: {empty}"
