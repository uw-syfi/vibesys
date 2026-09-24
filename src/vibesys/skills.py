"""VibeSys skill discovery and metadata validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

import vs_agent.api as _agent_api
from vibesys.constants import PROJECT_ROOT, ComputeBackend, DomainName
from vs_agent.api import SkillSelection

NULL_SKILL_SELECTION = _agent_api.NULL_SKILL_SELECTION

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vibesys.schemas import SkillResourceSelection

SIDECAR_NAME = ".vibesys.toml"
_FRONTMATTER_DELIMITER = "---"
_MATERIALIZATION_EXCLUDED_NAMES = frozenset({".git", "repos", "__pycache__"})

# Files every ``references/platforms/<backend>/`` directory must provide.
# ``floor.md`` is the per-backend optimization floor, which is genuinely
# different per platform and must never fall back to another's.
PLATFORM_SKELETON: tuple[str, ...] = ("floor.md", "hardware.md", "profiler.md")

# Parent path of the per-backend directories inside a skill.
PLATFORMS_PARENT: tuple[str, str] = ("references", "platforms")


def foreign_platform_names(compute_backend: ComputeBackend | None) -> frozenset[str]:
    """Return the ``platforms/<backend>/`` directory names to prune.

    Empty when no backend is selected (copy the tree intact). Otherwise every
    known :class:`ComputeBackend` value except the selected one — the agent
    must not be able to read another platform's guidance, because applying one
    platform's optimization floor to another produces wrong work rather than
    merely irrelevant reading.
    """
    if compute_backend is None:
        return frozenset()
    return frozenset(b.value for b in ComputeBackend if b is not compute_backend)


def is_platforms_parent(directory: Path | str) -> bool:
    """True when *directory* is the ``references/platforms`` dir of a skill.

    Pruning keys on the parent path rather than on directory name so an
    unrelated directory that happens to be called e.g. ``cpu`` is never
    dropped.
    """
    return Path(directory).parts[-2:] == PLATFORMS_PARENT


def platform_skill_selection(compute_backend: ComputeBackend | None) -> SkillSelection:
    """Build the ``SkillSelection`` that prunes foreign ``platforms/<backend>/`` dirs.

    This is the policy half of skill materialization: it knows about compute
    backends. The agent package (``vs_agent.cli_common.materialize_skills``)
    only knows how to apply a caller-supplied ``SkillSelection``, not what a
    compute backend is.
    """
    foreign = foreign_platform_names(compute_backend)

    def _skip_dir(src_dir: str, names: list[str]) -> set[str]:
        if not foreign or not is_platforms_parent(src_dir):
            return set()
        return {name for name in names if name in foreign}

    return SkillSelection(skip_dir=_skip_dir)


class SkillMetadataError(ValueError):
    """Raised when a skill or VibeSys sidecar metadata is malformed."""


@dataclass(frozen=True)
class SkillRule:
    """One path-scoped rule from a ``.vibesys.toml`` sidecar."""

    sidecar_path: Path
    raw_path: str
    target_path: Path
    backends: tuple[ComputeBackend, ...] | None
    domains: tuple[DomainName, ...] | None

    @property
    def specificity(self) -> int:
        """Rule precedence: deeper target paths are more specific."""
        return len(self.target_path.parts)

    def applies_to(self, skill_dir: Path) -> bool:
        """Return whether ``skill_dir`` is within this rule's target path."""
        try:
            skill_dir.resolve().relative_to(self.target_path)
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class SkillMetadata:
    """Effective VibeSys metadata for one discovered skill."""

    skill_dir: Path
    backends: tuple[ComputeBackend, ...] | None
    domains: tuple[DomainName, ...] | None
    rule: SkillRule | None = None

    def supports_backend(self, backend: ComputeBackend) -> bool:
        """True when this skill should be loaded for *backend*."""
        return self.backends is None or backend in self.backends

    def supports_domain(self, domain: DomainName) -> bool:
        """True when this skill should be loaded for *domain*."""
        return self.domains is None or domain in self.domains


@dataclass(frozen=True)
class SkillCatalogEntry:
    """One installed skill addressable by an agent-visible skill name."""

    name: str
    source_dir: Path

    @property
    def router_path(self) -> str:
        """Workspace-relative path to this skill's router."""
        return f"{self.name}/SKILL.md"


@dataclass(frozen=True)
class ResolvedSkillSelection:
    """Validated, agent-visible paths for one advisory skill selection."""

    skill: str
    router_path: str
    resource_paths: tuple[str, ...]
    purpose: str


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


def _parse_domains(sidecar_path: Path, raw_domains: object) -> tuple[DomainName, ...] | None:
    if raw_domains is None:
        return None
    if not isinstance(raw_domains, list):
        raise _metadata_error(sidecar_path, "`domains` must be a list")

    known = {domain.value: domain for domain in DomainName}
    domains: list[DomainName] = []
    invalid: list[object] = []
    for value in raw_domains:
        if not isinstance(value, str) or value not in known:
            invalid.append(value)
            continue
        domains.append(known[value])

    if invalid:
        allowed = ", ".join(sorted(known))
        bad = ", ".join(repr(v) for v in invalid)
        raise _metadata_error(
            sidecar_path,
            f"`domains` contains invalid domain name(s): {bad}. Allowed: {allowed}",
        )

    return tuple(dict.fromkeys(domains))


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
    """Load and validate one ``.vibesys.toml`` sidecar."""
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

        allowed_rule = {"path", "backends", "domains"}
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
                domains=_parse_domains(sidecar_path, raw_rule.get("domains")),
            )
        )
    return rules


def _is_in_hidden_dir(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts[:-1])


def discover_skill_dirs(root: Path) -> list[Path]:
    """Return skill directories under *root*.

    ``root`` may be one skill directory or a parent tree containing many skills.
    """
    if (root / "SKILL.md").is_file():
        return [root]
    return sorted({p.parent for p in root.rglob("SKILL.md") if not _is_in_hidden_dir(p, root)})


def discover_sidecar_rules(root: Path) -> list[SkillRule]:
    """Return all VibeSys sidecar rules under *root*."""
    return [
        rule
        for sidecar_path in sorted(root.rglob(SIDECAR_NAME))
        if not _is_in_hidden_dir(sidecar_path, root)
        for rule in load_sidecar_rules(sidecar_path)
    ]


def coerce_skill_root(raw: str | Path, *, project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve one configured skill source to a directory.

    A source may be a skill directory (containing ``SKILL.md``), a parent tree
    of many skills, or a single ``SKILL.md`` file (resolved to its containing
    skill directory).
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.exists():
        message = f"skill source path does not exist: {raw}"
        raise ValueError(message)
    if path.is_file():
        if path.name != "SKILL.md":
            _exception_message_2 = f"skill source file must be a SKILL.md file: {raw}"
            raise ValueError(_exception_message_2)
        return path.parent
    if not path.is_dir():
        _exception_message = f"skill source path is not a directory or SKILL.md file: {raw}"
        raise ValueError(_exception_message)
    return path


def build_skill_catalog(skill_dirs: Iterable[str | Path]) -> dict[str, SkillCatalogEntry]:
    """Build the catalog matching agent skill materialization semantics.

    Each input may be one skill directory or a parent containing several
    skills. Duplicate names use the last source, matching ``materialize_skills``.
    A skill's frontmatter name must match its materialized directory name so an
    outer-loop recommendation cannot resolve differently across providers.
    """
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
            catalog[name] = SkillCatalogEntry(name=name, source_dir=source_dir)
    return catalog


def _skill_resource_parts(resource: str) -> PurePosixPath | str:
    """Parse a safe skill-relative path, returning its diagnostic if invalid."""
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
    entry: SkillCatalogEntry,
    raw_resource: str,
) -> tuple[str | None, str | None]:
    """Resolve one skill-relative file to its agent-visible path and diagnostic."""
    parsed = _skill_resource_parts(raw_resource.strip())
    if isinstance(parsed, str):
        return None, parsed
    relative = parsed

    source_root = entry.source_dir.resolve()
    lexical_path = entry.source_dir.joinpath(*relative.parts)
    try:
        resolved_path = lexical_path.resolve(strict=True)
        resolved_path.relative_to(source_root)
    except FileNotFoundError:
        return None, "resource file does not exist"
    except (OSError, RuntimeError, ValueError):
        return None, "resource path escapes the skill root"
    if not resolved_path.is_file():
        return None, "resource path must identify a file"

    workspace_path = PurePosixPath(entry.name, *relative.parts).as_posix()
    return workspace_path, None


def resolve_skill_selections(
    selections: Sequence[SkillResourceSelection],
    catalog: dict[str, SkillCatalogEntry],
) -> tuple[list[ResolvedSkillSelection], list[str]]:
    """Validate advisory skill selections without turning them into gates.

    Unknown skills and unsafe or missing resources are omitted and returned as
    diagnostics. Valid resources survive alongside an invalid sibling. Repeated
    selections for one skill are merged in first-seen order to keep continuation
    prompts compact and deterministic.
    """
    merged: dict[str, tuple[str, list[str]]] = {}
    diagnostics: list[str] = []
    for index, selection in enumerate(selections, start=1):
        skill = selection.skill.strip()
        entry = catalog.get(skill)
        if entry is None:
            diagnostics.append(f"selection #{index}: unknown installed skill {skill!r}")
            continue

        if skill not in merged:
            merged[skill] = (selection.purpose.strip(), [])
        purpose, resources = merged[skill]
        for raw_resource in selection.resource_paths:
            workspace_path, error = _resolve_skill_resource(entry, raw_resource)
            if error is not None:
                diagnostics.append(
                    f"selection #{index} skill {skill!r} resource {raw_resource!r}: {error}"
                )
                continue
            if workspace_path is None:
                message = "skill resource resolver returned no path or diagnostic"
                raise RuntimeError(message)
            if workspace_path == entry.router_path or workspace_path in resources:
                continue
            resources.append(workspace_path)
        merged[skill] = (purpose, resources)

    resolved = [
        ResolvedSkillSelection(
            skill=skill,
            router_path=catalog[skill].router_path,
            resource_paths=tuple(resources),
            purpose=purpose,
        )
        for skill, (purpose, resources) in merged.items()
    ]
    return resolved, diagnostics


def effective_skill_metadata(skill_dir: Path, rules: list[SkillRule]) -> SkillMetadata:
    """Resolve winning VibeSys metadata for one skill directory."""
    # Validate standard Agent Skill frontmatter even though VibeSys routing is
    # stored out-of-band in sidecar files.
    load_skill_frontmatter(skill_dir)

    matches = [rule for rule in rules if rule.applies_to(skill_dir)]
    if not matches:
        return SkillMetadata(skill_dir=skill_dir, backends=None, domains=None)

    best_specificity = max(rule.specificity for rule in matches)
    winners = [rule for rule in matches if rule.specificity == best_specificity]
    constraints = {(rule.backends, rule.domains) for rule in winners}
    if len(constraints) > 1:
        locations = ", ".join(f"{rule.sidecar_path}:{rule.raw_path}" for rule in winners)
        raise _metadata_error(
            skill_dir / "SKILL.md",
            f"conflicting VibeSys rules at same specificity: {locations}",
        )

    winner = winners[0]
    return SkillMetadata(
        skill_dir=skill_dir,
        backends=winner.backends,
        domains=winner.domains,
        rule=winner,
    )


def resolve_skill_source_dirs(
    raw_dirs: list[str | Path] | None,
    *,
    backend: ComputeBackend,
    domain: DomainName,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Resolve configured skill roots to compatible skill directories.

    ``raw_dirs`` defines the candidate roots. Each discovered ``SKILL.md`` is
    validated, then included only if the effective VibeSys sidecar metadata
    supports the selected backend and domain. Skills with no matching rule are
    globally eligible and load for every backend and domain.
    """
    if not raw_dirs:
        return []

    resolved: dict[Path, None] = {}
    for raw in raw_dirs:
        root = coerce_skill_root(raw, project_root=project_root)
        rules = discover_sidecar_rules(root)
        for skill_dir in discover_skill_dirs(root):
            metadata = effective_skill_metadata(skill_dir, rules)
            if metadata.supports_backend(backend) and metadata.supports_domain(domain):
                resolved[skill_dir.resolve()] = None
    return [str(path) for path in resolved]


def validate_platform_layout(skill_dir: Path) -> None:
    """Validate a skill's ``references/platforms/`` tree, if it has one.

    Skills may carry per-backend guidance under
    ``references/platforms/<backend>/``. Two rules keep that tree honest:

    1. Every directory name must be a known :class:`ComputeBackend` value, so
       materialization can prune foreign platforms by literal name match.
    2. Every platform directory must contain the skeleton files. A missing
       one is a real gap — without this check it silently resolves to
       whichever platform happens to be most complete, which is how a
       platform ends up documented by another platform's guidance.
    """
    platforms_dir = skill_dir / "references" / "platforms"
    if not platforms_dir.is_dir():
        return

    known = {backend.value for backend in ComputeBackend}
    present = sorted(p for p in platforms_dir.iterdir() if p.is_dir())

    unknown = [p.name for p in present if p.name not in known]
    if unknown:
        raise _metadata_error(
            platforms_dir,
            f"unknown platform director{'y' if len(unknown) == 1 else 'ies'}: "
            f"{', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(known))}",
        )

    for platform_dir in present:
        missing = sorted(n for n in PLATFORM_SKELETON if not (platform_dir / n).is_file())
        if missing:
            raise _metadata_error(
                platform_dir,
                f"platform directory is missing required file(s): {', '.join(missing)}",
            )


def validate_skill_tree(root: Path) -> list[SkillMetadata]:
    """Validate every skill and VibeSys sidecar under *root*."""
    rules = discover_sidecar_rules(root)
    metadata = []
    for skill_dir in discover_skill_dirs(root):
        validate_platform_layout(skill_dir)
        metadata.append(effective_skill_metadata(skill_dir, rules))
    return metadata
