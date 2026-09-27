"""Public contracts for installed skill discovery and resource resolution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import SkillCatalogError, SkillResourceRequest
from vs_runtime.api.infrastructure import (
    BlockingOperations,
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    resolve_skill_resources,
)
from vs_runtime.api.infrastructure_skills import create_installed_skills

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


def test_installed_skills_resolve_through_the_runtime_capability(tmp_path: Path) -> None:
    _write_skill(tmp_path, "profiling")
    skills = create_installed_skills((tmp_path,), BlockingOperations())

    result = asyncio.run(
        skills.resolve(
            (
                SkillResourceRequest(
                    name="profiling",
                    resource_paths=("references/guide.md", "missing.md"),
                    purpose="inspect behavior",
                ),
                SkillResourceRequest(name="unknown", purpose="not installed"),
            )
        )
    )

    assert result.resolved[0].name == "profiling"
    assert result.resolved[0].resource_paths == ("profiling/references/guide.md",)
    assert len(result.diagnostics) == 2
    assert "resource file does not exist" in result.diagnostics[0]
    assert "unknown installed skill" in result.diagnostics[1]


def test_installed_skills_translate_invalid_catalog_to_contract_error(tmp_path: Path) -> None:
    skill = tmp_path / "profiling"
    skill.mkdir()
    (skill / "SKILL.md").write_text("missing frontmatter\n")
    skills = create_installed_skills((tmp_path,), BlockingOperations())

    with pytest.raises(SkillCatalogError, match="SkillMetadataError:"):
        asyncio.run(
            skills.resolve((SkillResourceRequest(name="profiling", purpose="inspect behavior"),))
        )


def test_installed_skills_report_when_no_sources_are_installed() -> None:
    skills = create_installed_skills((), BlockingOperations())

    result = asyncio.run(
        skills.resolve((SkillResourceRequest(name="profiling", purpose="inspect behavior"),))
    )

    assert result.resolved == ()
    assert result.diagnostics == ("no skill sources are installed",)
