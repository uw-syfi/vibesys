"""Discover and validate skill sources using orchestration-owned routing policy."""

from pathlib import Path

from vibesys.config import BUNDLED_RESOURCES
from vibesys.constants import PROJECT_ROOT, ComputeBackend, DomainName
from vibesys.orchestration.skill_selection import (
    SkillMetadata,
    coerce_skill_root,
    discover_sidecar_rules,
    effective_skill_metadata,
    validate_platform_layout,
)
from vs_runtime.api.infrastructure import discover_skill_dirs, load_skill_frontmatter


def resolve_skill_source_dirs(
    raw_dirs: list[str | Path] | None,
    *,
    backend: ComputeBackend,
    domain: DomainName,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Resolve configured skill roots to compatible validated skill directories."""
    if not raw_dirs:
        return []

    resolved: dict[Path, None] = {}
    for raw in raw_dirs:
        root = coerce_skill_root(raw, project_root=project_root)
        rules = discover_sidecar_rules(root)
        for skill_dir in discover_skill_dirs(root):
            load_skill_frontmatter(skill_dir)
            metadata = effective_skill_metadata(skill_dir, rules)
            if metadata.supports_backend(backend) and metadata.supports_domain(domain):
                resolved[skill_dir.resolve()] = None
    return [str(path) for path in resolved]


def chat_skill_dirs() -> tuple[Path, ...]:
    """The skills a run's chat agent is installed with: every skill of the bundled chat tree.

    The tree is separate from the optimization agents' (``skills/domains``), whose discovery
    has no way to scope a skill to one kind of agent, so a chat skill never reaches them. No
    sidecar applies: these skills teach steering and reading a run, on every backend and domain.
    Empty when no bundled tree is installed.
    """
    root = BUNDLED_RESOURCES.directory("chat-skills")
    if root is None:
        return ()
    skills = discover_skill_dirs(root)
    for skill_dir in skills:
        load_skill_frontmatter(skill_dir)
    return tuple(skills)


def validate_skill_tree(root: Path) -> list[SkillMetadata]:
    """Validate every skill and VibeSys sidecar under *root*."""
    rules = discover_sidecar_rules(root)
    metadata = []
    for skill_dir in discover_skill_dirs(root):
        validate_platform_layout(skill_dir)
        load_skill_frontmatter(skill_dir)
        metadata.append(effective_skill_metadata(skill_dir, rules))
    return metadata


__all__ = ["chat_skill_dirs", "resolve_skill_source_dirs", "validate_skill_tree"]
