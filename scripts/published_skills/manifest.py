"""The plugin marketplace manifest and its generated skill list."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.published_skills.skills import PUBLISHED_ROOT, discover

MANIFEST_PATH = Path(".claude-plugin/marketplace.json")
PLUGIN_NAME = "vibesys"
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


def _plugin_entry(manifest: dict[str, object]) -> dict[str, object]:
    plugins = manifest.get("plugins")
    entries = (
        [p for p in plugins if isinstance(p, dict) and p.get("name") == PLUGIN_NAME]
        if isinstance(plugins, list)
        else []
    )
    if len(entries) != 1:
        message = (
            f"{MANIFEST_PATH.as_posix()}: `plugins` must hold exactly one entry named "
            f"`{PLUGIN_NAME}`"
        )
        raise ManifestError(message)
    entry = entries[0]
    if entry.get("source") != PLUGIN_SOURCE:
        message = (
            f"{MANIFEST_PATH.as_posix()}: the `{PLUGIN_NAME}` entry's `source` must be "
            f"`{PLUGIN_SOURCE}` so that everything under it, and nothing else, is published"
        )
        raise ManifestError(message)
    if not isinstance(entry.get("version"), str) or not entry["version"]:
        message = (
            f"{MANIFEST_PATH.as_posix()}: the `{PLUGIN_NAME}` entry needs an explicit `version`; "
            "without one every commit looks like an update"
        )
        raise ManifestError(message)
    return entry


def render_manifest(root: Path) -> str:
    """The committed manifest with its skill list regenerated from the tree."""
    manifest = _load(root)
    _plugin_entry(manifest)["skills"] = [skill.path for skill in discover(root)]
    return json.dumps(manifest, indent=2) + "\n"


def check_manifest(root: Path) -> list[str]:
    """Findings for a manifest that differs from what the tree generates; empty when current."""
    try:
        expected = render_manifest(root)
    except ManifestError as exc:
        return [str(exc)]
    actual = (root / MANIFEST_PATH).read_text(encoding="utf-8")
    if actual == expected:
        return []
    return [
        f"{MANIFEST_PATH.as_posix()}: out of date with `skills/`; "
        "run `python3 -m scripts.published_skills --write` and commit the result"
    ]
