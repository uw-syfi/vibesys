"""Generic installed-skill discovery and safe resource resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from vs_runtime.contracts import (
    SkillFact,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_FRONTMATTER_DELIMITER = "---"
_MATERIALIZATION_EXCLUDED_NAMES = frozenset({".git", "repos", "__pycache__"})


class SkillMetadataError(ValueError):
    """Raised when standard agent skill metadata is malformed."""


@dataclass(frozen=True)
class SkillCatalogEntry:
    """One installed skill addressable by its agent-visible name."""

    name: str
    description: str
    source_dir: Path

    @property
    def router_path(self) -> str:
        """Return the workspace-relative path to this skill's router."""
        return f"{self.name}/SKILL.md"


def _metadata_error(path: Path, message: str) -> SkillMetadataError:
    return SkillMetadataError(f"{path}: {message}")


def load_skill_frontmatter(skill_dir: Path) -> dict[str, Any]:
    """Parse and validate standard YAML frontmatter from one ``SKILL.md``."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise _metadata_error(skill_md, "missing SKILL.md")

    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise _metadata_error(skill_md, "missing opening YAML frontmatter delimiter")

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == _FRONTMATTER_DELIMITER
        ),
        None,
    )
    if closing_index is None:
        raise _metadata_error(skill_md, "missing closing YAML frontmatter delimiter")

    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as exc:
        raise _metadata_error(skill_md, f"invalid YAML frontmatter: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise _metadata_error(skill_md, "YAML frontmatter must be a mapping")
    return parsed


def _is_in_hidden_dir(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts[:-1])


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return skill directories beneath one skill or parent tree."""
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted(
        {path.parent for path in root.rglob("SKILL.md") if not _is_in_hidden_dir(path, root)}
    )


def build_skill_catalog(skill_dirs: Iterable[str | Path]) -> dict[str, SkillCatalogEntry]:
    """Build a deterministic catalog, with later duplicate names winning."""
    catalog: dict[str, SkillCatalogEntry] = {}
    for raw_root in skill_dirs:
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            raise _metadata_error(root, "skill catalog root is not a directory")
        for skill_dir in discover_skill_dirs(root):
            source_dir = skill_dir.resolve()
            frontmatter = load_skill_frontmatter(source_dir)
            raw_name = frontmatter.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise _metadata_error(source_dir / "SKILL.md", "`name` must be a string")
            name = raw_name.strip()
            if name != source_dir.name:
                raise _metadata_error(
                    source_dir / "SKILL.md",
                    f"frontmatter name {name!r} must match directory name {source_dir.name!r}",
                )
            raw_description = frontmatter.get("description")
            if not isinstance(raw_description, str) or not raw_description.strip():
                raise _metadata_error(
                    source_dir / "SKILL.md", "`description` must be a non-empty string"
                )
            catalog[name] = SkillCatalogEntry(
                name=name,
                description=" ".join(raw_description.split()),
                source_dir=source_dir,
            )
    return catalog


def offered_skill_facts(skill_dirs: Iterable[str | Path]) -> tuple[SkillFact, ...]:
    """Return the prompt-visible facts for every skill in one installed catalog."""
    return tuple(
        SkillFact(name=entry.name, description=entry.description)
        for entry in build_skill_catalog(skill_dirs).values()
    )


__all__ = [
    "SkillCatalogEntry",
    "SkillMetadataError",
    "build_skill_catalog",
    "discover_skill_dirs",
    "load_skill_frontmatter",
    "offered_skill_facts",
]
