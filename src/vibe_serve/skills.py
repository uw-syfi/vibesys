"""VibeServe skill discovery and metadata validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vibe_serve.constants import PROJECT_ROOT, ComputeBackend

DEFAULT_SKILL_ROOTS: tuple[Path, ...] = (Path("resources/skills"),)
SIDECAR_NAME = ".vibeserve.toml"
_FRONTMATTER_DELIMITER = "---"


class SkillMetadataError(ValueError):
    """Raised when a skill or VibeServe sidecar metadata is malformed."""


@dataclass(frozen=True)
class SkillRule:
    """One path-scoped rule from a ``.vibeserve.toml`` sidecar."""

    sidecar_path: Path
    raw_path: str
    target_path: Path
    backends: tuple[ComputeBackend, ...] | None

    @property
    def specificity(self) -> int:
        """Rule precedence: deeper target paths are more specific."""
        return len(self.target_path.parts)

    def applies_to(self, skill_dir: Path) -> bool:
        try:
            skill_dir.resolve().relative_to(self.target_path)
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class SkillMetadata:
    """Effective VibeServe metadata for one discovered skill."""

    skill_dir: Path
    backends: tuple[ComputeBackend, ...] | None
    rule: SkillRule | None = None

    def supports_backend(self, backend: ComputeBackend) -> bool:
        """True when this skill should be loaded for *backend*."""
        return self.backends is None or backend in self.backends


def _metadata_error(path: Path, message: str) -> SkillMetadataError:
    return SkillMetadataError(f"{path}: {message}")


def load_skill_frontmatter(skill_dir: Path) -> dict[str, Any]:
    """Parse and validate standard YAML frontmatter from one ``SKILL.md``."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise _metadata_error(skill_md, "missing SKILL.md")

    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise _metadata_error(skill_md, "missing opening YAML frontmatter delimiter")

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIMITER:
            closing_index = index
            break
    if closing_index is None:
        raise _metadata_error(skill_md, "missing closing YAML frontmatter delimiter")

    raw = "\n".join(lines[1:closing_index])
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _metadata_error(skill_md, f"invalid YAML frontmatter: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise _metadata_error(skill_md, "YAML frontmatter must be a mapping")
    return parsed


def _parse_backends(sidecar_path: Path, raw_backends: object) -> tuple[ComputeBackend, ...] | None:
    if raw_backends is None:
        return None
    if not isinstance(raw_backends, list):
        raise _metadata_error(sidecar_path, "`backends` must be a list")

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
        raise _metadata_error(
            sidecar_path,
            f"`backends` contains invalid backend name(s): {bad}. Allowed: {allowed}",
        )

    # Keep author order but remove duplicates.
    return tuple(dict.fromkeys(backends))


def _parse_rule_path(sidecar_path: Path, raw_path: object) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _metadata_error(sidecar_path, "`rule.path` must be a non-empty string")

    rule_path = Path(raw_path)
    if rule_path.is_absolute() or ".." in rule_path.parts:
        raise _metadata_error(sidecar_path, "`rule.path` must be relative and stay in-tree")

    sidecar_dir = sidecar_path.parent.resolve()
    target = (sidecar_dir / rule_path).resolve()
    try:
        target.relative_to(sidecar_dir)
    except ValueError as exc:
        raise _metadata_error(sidecar_path, "`rule.path` must stay in-tree") from exc
    if not target.exists():
        raise _metadata_error(sidecar_path, f"`rule.path` does not exist: {raw_path!r}")

    return raw_path, target


def load_sidecar_rules(sidecar_path: Path) -> list[SkillRule]:
    """Load and validate one ``.vibeserve.toml`` sidecar."""
    try:
        data = tomllib.loads(sidecar_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _metadata_error(sidecar_path, f"invalid TOML: {exc}") from exc

    allowed_top = {"rule"}
    unknown_top = sorted(set(data) - allowed_top)
    if unknown_top:
        raise _metadata_error(sidecar_path, f"unknown top-level key(s): {', '.join(unknown_top)}")

    raw_rules = data.get("rule")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _metadata_error(sidecar_path, "expected at least one [[rule]] table")

    rules: list[SkillRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise _metadata_error(sidecar_path, f"rule #{index} must be a table")

        allowed_rule = {"path", "backends"}
        unknown_rule = sorted(set(raw_rule) - allowed_rule)
        if unknown_rule:
            raise _metadata_error(
                sidecar_path,
                f"rule #{index} has unknown key(s): {', '.join(unknown_rule)}",
            )

        raw_path, target = _parse_rule_path(sidecar_path, raw_rule.get("path"))
        rules.append(
            SkillRule(
                sidecar_path=sidecar_path,
                raw_path=raw_path,
                target_path=target,
                backends=_parse_backends(sidecar_path, raw_rule.get("backends")),
            )
        )
    return rules


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return skill directories under *root*.

    ``root`` may be one skill directory or a parent tree containing many skills.
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted({p.parent for p in root.rglob("SKILL.md")})


def discover_sidecar_rules(root: Path) -> list[SkillRule]:
    """Return all VibeServe sidecar rules under *root*."""
    return [
        rule
        for sidecar_path in sorted(root.rglob(SIDECAR_NAME))
        for rule in load_sidecar_rules(sidecar_path)
    ]


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


def effective_skill_metadata(skill_dir: Path, rules: list[SkillRule]) -> SkillMetadata:
    """Resolve winning VibeServe metadata for one skill directory."""
    # Validate standard Agent Skill frontmatter even though VibeServe routing is
    # stored out-of-band in sidecar files.
    load_skill_frontmatter(skill_dir)

    matches = [rule for rule in rules if rule.applies_to(skill_dir)]
    if not matches:
        return SkillMetadata(skill_dir=skill_dir, backends=None)

    best_specificity = max(rule.specificity for rule in matches)
    winners = [rule for rule in matches if rule.specificity == best_specificity]
    backend_sets = {rule.backends for rule in winners}
    if len(backend_sets) > 1:
        locations = ", ".join(f"{rule.sidecar_path}:{rule.raw_path}" for rule in winners)
        raise _metadata_error(
            skill_dir / "SKILL.md",
            f"conflicting VibeServe rules at same specificity: {locations}",
        )

    winner = winners[0]
    return SkillMetadata(skill_dir=skill_dir, backends=winner.backends, rule=winner)


def resolve_skill_source_dirs(
    raw_dirs: list[str | Path] | None,
    *,
    backend: ComputeBackend,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Resolve configured skill roots to backend-compatible skill directories.

    ``raw_dirs`` defines the candidate roots. Each discovered ``SKILL.md`` is
    validated, then included only if the effective VibeServe sidecar metadata
    supports the selected backend. Skills with no matching rule are
    backend-agnostic and load for every backend.
    """
    if not raw_dirs:
        return []

    resolved: dict[Path, None] = {}
    for raw in raw_dirs:
        root = coerce_skill_root(raw, project_root=project_root)
        rules = discover_sidecar_rules(root)
        for skill_dir in discover_skill_dirs(root):
            metadata = effective_skill_metadata(skill_dir, rules)
            if metadata.supports_backend(backend):
                resolved[skill_dir.resolve()] = None
    return [str(path) for path in resolved]


def validate_skill_tree(root: Path) -> list[SkillMetadata]:
    """Validate every skill and VibeServe sidecar under *root*."""
    rules = discover_sidecar_rules(root)
    return [effective_skill_metadata(skill_dir, rules) for skill_dir in discover_skill_dirs(root)]
