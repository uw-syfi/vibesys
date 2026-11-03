"""The Claude Code plugin's published surface: the marketplace manifest and its skills.

Everything under ``skills/`` ships through the plugins of ``PLUGIN_SKILLS`` and nothing outside
it does. Each plugin keeps the one source root and differs only by its skill list, which is
derived from the tree rather than written by hand, and every listed skill is checked against the
Agent Skills specification.

Stdlib only apart from the frontmatter reader it shares with ``scripts.check_skills``, so CI's
always-on job can run it without installing the environment.

    python3 -m scripts.published_skills            # check; exit 1 on any finding
    python3 -m scripts.published_skills --write    # rewrite the manifest's skill lists
"""

from scripts.published_skills.findings import Finding
from scripts.published_skills.harness import HARNESS_HEADINGS
from scripts.published_skills.manifest import (
    MANIFEST_PATH,
    ManifestError,
    check_manifest,
    render_manifest,
)
from scripts.published_skills.plugins import PLUGIN_SKILLS
from scripts.published_skills.skills import (
    SPEC_FIELDS,
    VIBESYS_TOOL_NAMES,
    PublishedSkill,
    check_skills,
    discover,
)

__all__ = [
    "HARNESS_HEADINGS",
    "MANIFEST_PATH",
    "PLUGIN_SKILLS",
    "SPEC_FIELDS",
    "VIBESYS_TOOL_NAMES",
    "Finding",
    "ManifestError",
    "PublishedSkill",
    "check_manifest",
    "check_skills",
    "discover",
    "render_manifest",
]
