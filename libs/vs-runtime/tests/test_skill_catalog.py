"""Public contracts for installed skill discovery and resource resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import SkillFact
from vs_runtime.api.infrastructure import (
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    offered_skill_facts,
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


def test_catalog_requires_a_description(tmp_path: Path) -> None:
    skill = tmp_path / "bare"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: bare\n---\n")

    with pytest.raises(SkillMetadataError, match="`description` must be a non-empty string"):
        offered_skill_facts((tmp_path,))


def test_offered_facts_name_every_skill_with_its_description_on_one_line(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "profiling")
    folded = tmp_path / "folded"
    folded.mkdir()
    (folded / "SKILL.md").write_text(
        "---\nname: folded\ndescription: |\n  First line.\n  Second line.\n---\n"
    )

    assert offered_skill_facts((tmp_path,)) == (
        SkillFact(name="folded", description="First line. Second line."),
        SkillFact(name="profiling", description="Test."),
    )
    assert offered_skill_facts(()) == ()
