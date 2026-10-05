"""Every registered plugin owns its prompt code and resources.

Plugin declarations live in ``vibesys.orchestration.<folder>``. A plugin may
use a ``prompts.py`` module or a ``prompts/`` package; neither requires a
duplicate central ``prompts/loops/<strategy>/`` directory.
"""

from __future__ import annotations

from pathlib import Path

from launch import built_in_orchestrations

_ORCHESTRATION = Path(__file__).parents[3].resolve() / "src" / "vibesys" / "orchestration"


def _registered_strategy_folders() -> set[str]:
    registry = built_in_orchestrations()
    folders = set()
    for registration in registry._registrations.values():  # noqa: SLF001  # LW-040196 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        plugin = registration.plugin
        if plugin.core is not None:
            # A core policy has no orchestrate function: its prompt folder names its strategy.
            location = Path(plugin.core.prompt_templates).resolve().relative_to(_ORCHESTRATION)
            folders.add(location.parts[0])
            continue
        module = plugin.orchestrate.__module__
        prefix = "vibesys.orchestration."
        assert module.startswith(prefix), f"registered policy {module!r} is not under {prefix}"
        folders.add(module.split(".")[2])
    return folders


def test_every_registered_strategy_has_a_local_prompt_owner() -> None:
    folders = _registered_strategy_folders()
    orchestration = _ORCHESTRATION
    missing = sorted(
        folder
        for folder in folders
        if not (orchestration / folder / "prompts.py").is_file()
        and not (orchestration / folder / "prompts").is_dir()
    )
    assert not missing, f"registered strategies with no plugin-local prompt owner: {missing}"
