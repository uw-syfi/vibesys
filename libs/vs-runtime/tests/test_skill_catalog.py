"""Public contracts for installed skill discovery and resource resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import SkillResourceRequest
from vs_runtime.api.infrastructure import (
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    resolve_skill_resources,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    skill = root / name
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {frontmatter_name or name}\ndescription: Test.\n---\n"
    )
    (skill / "references" / "guide.md").write_text("Guide.\n")
    return skill


def test_discovery_skips_hidden_parent_trees(tmp_path: Path) -> None:
    visible = _write_skill(tmp_path, "visible")
    _write_skill(tmp_path / ".hidden", "hidden")

    assert discover_skill_dirs(tmp_path) == [visible]


def test_catalog_requires_frontmatter_name_to_match_directory(tmp_path: Path) -> None:
    _write_skill(tmp_path, "actual", frontmatter_name="different")

    with pytest.raises(SkillMetadataError, match="must match directory name"):
        build_skill_catalog((tmp_path,))


def test_resource_resolution_preserves_partial_success_and_blocks_escapes(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "profiling")
    catalog = build_skill_catalog((tmp_path,))

    result = resolve_skill_resources(
        (
            SkillResourceRequest(
                name="profiling",
                resource_paths=("references/guide.md", "missing.md", "../secret"),
                purpose="inspect behavior",
            ),
            SkillResourceRequest(name="unknown", purpose="not installed"),
        ),
        catalog,
    )

    assert result.resolved[0].name == "profiling"
    assert result.resolved[0].router_path == "profiling/SKILL.md"
    assert result.resolved[0].resource_paths == ("profiling/references/guide.md",)
    assert len(result.diagnostics) == 3
    assert "resource file does not exist" in result.diagnostics[0]
    assert "must be relative" in result.diagnostics[1]
    assert "unknown installed skill" in result.diagnostics[2]
