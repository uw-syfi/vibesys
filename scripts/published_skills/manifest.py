"""The plugin marketplace manifest and its generated skill list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.published_skills.plugins import PLUGIN_SKILLS
from scripts.published_skills.skills import PUBLISHED_ROOT, discover

if TYPE_CHECKING:
    from collections.abc import Mapping

MANIFEST_PATH = Path(".claude-plugin/marketplace.json")
PLUGIN_SOURCE = f"./{PUBLISHED_ROOT.as_posix()}"


class ManifestError(ValueError):
    """The committed manifest cannot be read or lacks what the generator relies on."""


def _load(root: Path) -> dict[str, object]:
    path = root / MANIFEST_PATH
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        message = f"{MANIFEST_PATH.as_posix()}: missing; create it with the plugin entry first"
        raise ManifestError(message) from None
    except json.JSONDecodeError as exc:
        message = f"{MANIFEST_PATH.as_posix()}: not valid JSON ({exc})"
        raise ManifestError(message) from exc
    if not isinstance(loaded, dict):
        message = f"{MANIFEST_PATH.as_posix()}: the top level must be an object"
        raise ManifestError(message)
    return loaded


def _plugin_entries(
    manifest: dict[str, object], plugins: Mapping[str, tuple[str, ...]]
) -> dict[str, dict[str, object]]:
    """The manifest's entries by name, which must be exactly the table's plugins."""
    listed = manifest.get("plugins")
    entries = [p for p in listed if isinstance(p, dict)] if isinstance(listed, list) else []
    names = [entry.get("name") for entry in entries]
    duplicated = sorted({str(n) for n in names if names.count(n) > 1})
    missing = sorted(set(plugins) - set(names))
    extra = sorted(str(n) for n in set(names) - set(plugins))
    if duplicated or missing or extra:
        problems = [
            f"{label} {', '.join(f'`{n}`' for n in found)}"
            for label, found in (("duplicate", duplicated), ("missing", missing), ("extra", extra))
            if found
        ]
        message = (
            f"{MANIFEST_PATH.as_posix()}: `plugins` must hold exactly one entry for each of "
            f"{', '.join(f'`{n}`' for n in plugins)}; {'; '.join(problems)}"
        )
        raise ManifestError(message)
    by_name = {str(entry["name"]): entry for entry in entries}
    for name, entry in by_name.items():
        _check_entry(name, entry)
    return by_name


def _check_entry(name: str, entry: dict[str, object]) -> None:
    if entry.get("source") != PLUGIN_SOURCE:
        message = (
            f"{MANIFEST_PATH.as_posix()}: the `{name}` entry's `source` must be "
            f"`{PLUGIN_SOURCE}` so that everything under it, and nothing else, is published"
        )
        raise ManifestError(message)
    if not isinstance(entry.get("version"), str) or not entry["version"]:
        message = (
            f"{MANIFEST_PATH.as_posix()}: the `{name}` entry needs an explicit `version`; "
            "without one every commit looks like an update"
        )
        raise ManifestError(message)


def _belongs(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _plugin_skills(
    paths: list[str], plugins: Mapping[str, tuple[str, ...]]
) -> dict[str, list[str]]:
    """Each plugin's skills in discovery order; every skill and every prefix must be used."""
    dangling = [
        f"`{prefix}` of `{name}`"
        for name, prefixes in plugins.items()
        for prefix in prefixes
        if not any(_belongs(path, prefix) for path in paths)
    ]
    if dangling:
        message = f"plugin table: no skill lies under {', '.join(dangling)}"
        raise ManifestError(message)
    unassigned = [
        path
        for path in paths
        if not any(_belongs(path, prefix) for prefixes in plugins.values() for prefix in prefixes)
    ]
    if unassigned:
        message = (
            f"plugin table: skill(s) {', '.join(f'`{p}`' for p in unassigned)} belong to no "
            "plugin; list their folder in scripts/published_skills/plugins.py"
        )
        raise ManifestError(message)
    return {
        name: [p for p in paths if any(_belongs(p, prefix) for prefix in prefixes)]
        for name, prefixes in plugins.items()
    }


def render_manifest(root: Path, plugins: Mapping[str, tuple[str, ...]] = PLUGIN_SKILLS) -> str:
    """The committed manifest with each plugin's skill list regenerated from the tree."""
    manifest = _load(root)
    entries = _plugin_entries(manifest, plugins)
    lists = _plugin_skills([skill.path for skill in discover(root)], plugins)
    for name, skills in lists.items():
        entries[name]["skills"] = skills
    return json.dumps(manifest, indent=2) + "\n"


def check_manifest(
    root: Path, plugins: Mapping[str, tuple[str, ...]] = PLUGIN_SKILLS
) -> list[str]:
    """Findings for a manifest that differs from what the tree generates; empty when current."""
    try:
        expected = render_manifest(root, plugins)
    except ManifestError as exc:
        return [str(exc)]
    actual = (root / MANIFEST_PATH).read_text(encoding="utf-8")
    if actual == expected:
        return []
    return [
        f"{MANIFEST_PATH.as_posix()}: out of date with `skills/`; "
        "run `python3 -m scripts.published_skills --write` and commit the result"
    ]
