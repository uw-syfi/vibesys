"""VibeServe skill discovery and metadata validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vibe_serve.constants import PROJECT_ROOT, ComputeBackend

DEFAULT_SKILL_ROOTS: tuple[Path, ...] = (Path("resources/skills"),)
_FRONTMATTER_DELIMITER = "---"
_VIBESERVE_KEY = "vibeserve"
_BACKENDS_KEY = "backends"


class SkillMetadataError(ValueError):
    """Raised when a skill's VibeServe metadata is malformed."""


@dataclass(frozen=True)
class SkillMetadata:
    """Parsed metadata from a skill's ``SKILL.md`` frontmatter."""

    skill_dir: Path
    frontmatter: dict[str, Any]
    backends: tuple[ComputeBackend, ...] | None

    def supports_backend(self, backend: ComputeBackend) -> bool:
        """True when this skill should be loaded for *backend*."""
        return self.backends is None or backend in self.backends


def _frontmatter_error(path: Path, message: str) -> SkillMetadataError:
    return SkillMetadataError(f"{path}: {message}")


def _extract_frontmatter(skill_md: Path) -> dict[str, Any]:
    """Parse YAML frontmatter from *skill_md*.

    Agent Skills are expected to start with YAML frontmatter. VibeServe keeps
    its own routing metadata in the optional, namespaced ``vibeserve`` key.
    """
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise _frontmatter_error(skill_md, "missing opening YAML frontmatter delimiter")

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        raise _frontmatter_error(skill_md, "missing closing YAML frontmatter delimiter")

    raw = "\n".join(lines[1:closing_index])
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _frontmatter_error(skill_md, f"invalid YAML frontmatter: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise _frontmatter_error(skill_md, "YAML frontmatter must be a mapping")
    return parsed


def _parse_backend_list(
    skill_md: Path, frontmatter: dict[str, Any]
) -> tuple[ComputeBackend, ...] | None:
    vibeserve = frontmatter.get(_VIBESERVE_KEY)
    if vibeserve is None:
        return None
    if not isinstance(vibeserve, dict):
        raise _frontmatter_error(skill_md, "`vibeserve` metadata must be a mapping")

    raw_backends = vibeserve.get(_BACKENDS_KEY)
    if raw_backends is None:
        return None
    if not isinstance(raw_backends, list):
        raise _frontmatter_error(skill_md, "`vibeserve.backends` must be a list")

    known = {backend.value: backend for backend in ComputeBackend}
    backends: list[ComputeBackend] = []
    invalid: list[object] = []
    for value in raw_backends:
        if not isinstance(value, str) or value not in known:
            invalid.append(value)
            continue
        backends.append(known[value])

    if invalid:
        allowed = ", ".join(sorted(known))
        bad = ", ".join(repr(v) for v in invalid)
        raise _frontmatter_error(
            skill_md,
            f"`vibeserve.backends` contains invalid backend name(s): {bad}. Allowed: {allowed}",
        )

    # Keep author order but remove duplicates.
    return tuple(dict.fromkeys(backends))


def load_skill_metadata(skill_dir: Path) -> SkillMetadata:
    """Load and validate VibeServe metadata for one skill directory."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise _frontmatter_error(skill_md, "missing SKILL.md")
    frontmatter = _extract_frontmatter(skill_md)
    return SkillMetadata(
        skill_dir=skill_dir,
        frontmatter=frontmatter,
        backends=_parse_backend_list(skill_md, frontmatter),
    )


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return skill directories under *root*.

    ``root`` may be one skill directory or a parent tree containing many skills.
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted({p.parent for p in root.rglob("SKILL.md")})


def coerce_skill_root(raw: str | Path, *, project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve one configured skill root path."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"--skills-dir path does not exist: {raw}")
    if not path.is_dir():
        raise ValueError(f"--skills-dir path is not a directory: {raw}")
    return path


def resolve_skill_source_dirs(
    raw_dirs: list[str | Path] | None,
    *,
    backend: ComputeBackend,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Resolve configured skill roots to backend-compatible skill directories.

    ``raw_dirs`` defines the candidate roots. Each discovered ``SKILL.md`` is
    validated, then included only if its optional ``vibeserve.backends`` metadata
    includes the selected backend. Skills without VibeServe backend metadata are
    backend-agnostic and load for every backend.
    """
    if not raw_dirs:
        return []

    resolved: dict[Path, None] = {}
    for raw in raw_dirs:
        root = coerce_skill_root(raw, project_root=project_root)
        for skill_dir in discover_skill_dirs(root):
            metadata = load_skill_metadata(skill_dir)
            if metadata.supports_backend(backend):
                resolved[skill_dir.resolve()] = None
    return [str(path) for path in resolved]


def validate_skill_tree(root: Path) -> list[SkillMetadata]:
    """Validate every skill under *root* and return parsed metadata."""
    return [load_skill_metadata(skill_dir) for skill_dir in discover_skill_dirs(root)]
