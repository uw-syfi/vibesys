"""The Claude Code plugin's published surface: the marketplace manifest and its skills.

Everything under ``skills/`` ships in the ``vibesys`` plugin and nothing outside it does, so
the manifest's skill list is derived from the tree rather than written by hand, and every
listed skill is checked against the Claude Code skill limits.

Stdlib only apart from the frontmatter reader it shares with ``scripts.check_skills``, so CI's
always-on job can run it without installing the environment.

    python3 -m scripts.published_skills            # check; exit 1 on any finding
    python3 -m scripts.published_skills --write    # rewrite the manifest's skill list
"""

from scripts.published_skills.manifest import (
    MANIFEST_PATH,
    ManifestError,
    check_manifest,
    render_manifest,
)
from scripts.published_skills.skills import (
    VIBESYS_TOOL_NAMES,
    Finding,
    PublishedSkill,
    check_skills,
    discover,
)

__all__ = [
    "MANIFEST_PATH",
    "VIBESYS_TOOL_NAMES",
    "Finding",
    "ManifestError",
    "PublishedSkill",
    "check_manifest",
    "check_skills",
    "discover",
    "render_manifest",
]
