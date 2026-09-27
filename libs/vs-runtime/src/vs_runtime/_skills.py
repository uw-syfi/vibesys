"""Generic installed-skill discovery and safe resource resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from vs_runtime.contracts import (
    ResolvedSkillResources,
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
    Skills,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_runtime._run_host import BlockingOperations

_FRONTMATTER_DELIMITER = "---"
_MATERIALIZATION_EXCLUDED_NAMES = frozenset({".git", "repos", "__pycache__"})


class SkillMetadataError(ValueError):
    """Raised when standard agent skill metadata is malformed."""


@dataclass(frozen=True)
class SkillCatalogEntry:
    """One installed skill addressable by its agent-visible name."""

    name: str
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
            raw_name = load_skill_frontmatter(source_dir).get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise _metadata_error(source_dir / "SKILL.md", "`name` must be a string")
            name = raw_name.strip()
            if name != source_dir.name:
                raise _metadata_error(
                    source_dir / "SKILL.md",
                    f"frontmatter name {name!r} must match directory name {source_dir.name!r}",
                )
            catalog[name] = SkillCatalogEntry(name=name, source_dir=source_dir)
    return catalog


def _skill_resource_parts(resource: str) -> PurePosixPath | str:
    if not resource:
        return "resource path must be a non-empty string"
    if "\\" in resource:
        return "resource path must use POSIX separators"
    relative = PurePosixPath(resource)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return "resource path must be relative and stay within the skill"
    if any(part in _MATERIALIZATION_EXCLUDED_NAMES for part in relative.parts):
        return "resource path is excluded from agent skill materialization"
    return relative


def _resolve_skill_resource(
    entry: SkillCatalogEntry, raw_resource: str
) -> tuple[str | None, str | None]:
    parsed = _skill_resource_parts(raw_resource.strip())
    if isinstance(parsed, str):
        return None, parsed
    source_root = entry.source_dir.resolve()
    try:
        resolved_path = entry.source_dir.joinpath(*parsed.parts).resolve(strict=True)
        resolved_path.relative_to(source_root)
    except FileNotFoundError:
        return None, "resource file does not exist"
    except (OSError, RuntimeError, ValueError):
        return None, "resource path escapes the skill root"
    if not resolved_path.is_file():
        return None, "resource path must identify a file"
    return PurePosixPath(entry.name, *parsed.parts).as_posix(), None


def resolve_skill_resources(
    requests: Sequence[SkillResourceRequest],
    catalog: dict[str, SkillCatalogEntry],
) -> SkillResolution:
    """Resolve advisory skill resources, preserving partial valid selections."""
    merged: dict[str, tuple[str, list[str]]] = {}
    diagnostics: list[str] = []
    for index, request in enumerate(requests, start=1):
        name = request.name.strip()
        entry = catalog.get(name)
        if entry is None:
            diagnostics.append(f"selection #{index}: unknown installed skill {name!r}")
            continue
        if name not in merged:
            merged[name] = (request.purpose.strip(), [])
        purpose, resources = merged[name]
        for raw_resource in request.resource_paths:
            workspace_path, error = _resolve_skill_resource(entry, raw_resource)
            if error is not None:
                diagnostics.append(
                    f"selection #{index} skill {name!r} resource {raw_resource!r}: {error}"
                )
                continue
            if workspace_path is None:
                message = "skill resource resolver returned no path or diagnostic"
                raise RuntimeError(message)
            if workspace_path != entry.router_path and workspace_path not in resources:
                resources.append(workspace_path)
        merged[name] = (purpose, resources)

    return SkillResolution(
        resolved=tuple(
            ResolvedSkillResources(
                name=name,
                router_path=catalog[name].router_path,
                resource_paths=tuple(resources),
                purpose=purpose,
            )
            for name, (purpose, resources) in merged.items()
        ),
        diagnostics=tuple(diagnostics),
    )


class _InstalledSkills:
    """Resolve policy-owned requests against one run's installed catalog."""

    def __init__(
        self,
        source_paths: tuple[Path, ...],
        blocking: BlockingOperations,
    ) -> None:
        self._source_paths = source_paths
        self._blocking = blocking

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Return partial valid selections, diagnostics, or a catalog failure."""
        if not requests:
            return SkillResolution()
        if not self._source_paths:
            return SkillResolution(diagnostics=("no skill sources are installed",))
        try:
            return await self._blocking.run(_resolve_installed_skills, requests, self._source_paths)
        except (OSError, ValueError) as error:
            detail = f"{type(error).__name__}: {error}"
            raise SkillCatalogError(detail) from error


def _resolve_installed_skills(
    requests: tuple[SkillResourceRequest, ...], sources: tuple[Path, ...]
) -> SkillResolution:
    return resolve_skill_resources(requests, build_skill_catalog(sources))


def create_installed_skills(source_paths: tuple[Path, ...], blocking: BlockingOperations) -> Skills:
    """Create the runtime capability for resolving installed skill resources."""
    return _InstalledSkills(source_paths, blocking)


__all__ = [
    "SkillCatalogEntry",
    "SkillMetadataError",
    "build_skill_catalog",
    "create_installed_skills",
    "discover_skill_dirs",
    "load_skill_frontmatter",
    "resolve_skill_resources",
]
