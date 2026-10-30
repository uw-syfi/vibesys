"""Validate the installed skills' public metadata contract."""

import re
from pathlib import Path

import pytest
import yaml

SKILLS = sorted(Path(__file__).resolve().parents[2].glob("*/SKILL.md"))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter_loads_with_compatible_names_and_descriptions(path: Path) -> None:
    frontmatter = path.read_text().split("---", 2)[1]
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == path.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert len(metadata["name"]) <= 64
    assert isinstance(metadata["description"], str)
    assert 0 < len(metadata["description"]) <= 1024
