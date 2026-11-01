"""Find the published skills and check each against the Agent Skills specification.

The specification (https://agentskills.io/specification) is what Claude Code, Codex and the
``skills`` CLI all read, so a skill that passes here installs in each of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from scripts.check_skills import FrontmatterError, parse_frontmatter
from scripts.published_skills.findings import Finding
from scripts.published_skills.harness import check_harness_files

PUBLISHED_ROOT = Path("skills")
"""The plugin source root, relative to the repository: everything inside ships."""

TREES = ("search", "domains")
"""The subtrees of the published root that hold skills, in manifest order."""

SKILL_FILE = "SKILL.md"
SKIPPED_DIRECTORIES = frozenset({".git", "repos", "node_modules", "__pycache__"})
VENDOR_MARKER = "update.sh"
"""A top-level domain folder with this file re-vendors an upstream pack, byte for byte."""

NAME_PATTERN = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
SPEC_FIELDS = frozenset(
    {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
)
"""The frontmatter fields the specification defines; the reference validator rejects others."""
STRING_FIELDS = ("license", "compatibility", "allowed-tools")

VIBESYS_TOOL_NAMES = ("submit_evaluation", "validate_evaluation_wait", "read_queue_status")
"""Agent tools only a VibeSys run offers; a domain skill that names one fails outside a run.

A test pins this tuple to the product's own tool-name constants.
"""

DOMAINS_TREE = "domains"
MIN_VENDORED_DEPTH = 2
"""A vendored skill sits below its pack folder: ``domains/<pack>/.../<skill>``."""


@dataclass(frozen=True, slots=True)
class PublishedSkill:
    """One skill folder under the published root."""

    path: str
    """Folder path relative to the published root, with a leading ``./``."""
    vendored: bool
    """Whether the folder belongs to a pack that must stay byte-identical with upstream."""

    @property
    def folder(self) -> str:
        """The folder's own name, which the skill's ``name`` must equal."""
        return self.path.rsplit("/", 1)[-1]


def discover(root: Path) -> list[PublishedSkill]:
    """Every skill folder under ``root/skills/{search,domains}``, sorted by path.

    A folder holding ``SKILL.md`` is a skill; its own subfolders are its content, not more
    skills. Folders in ``SKIPPED_DIRECTORIES`` and hidden folders are not searched.
    """
    base = root / PUBLISHED_ROOT
    found: list[PublishedSkill] = []
    for tree in TREES:
        top = base / tree
        if top.is_dir():
            _walk(top, base, found)
    return sorted(found, key=lambda skill: skill.path)


def _walk(folder: Path, base: Path, found: list[PublishedSkill]) -> None:
    if (folder / SKILL_FILE).is_file():
        relative = folder.relative_to(base)
        found.append(PublishedSkill(f"./{relative.as_posix()}", _is_vendored(relative, base)))
        return
    for child in sorted(folder.iterdir()):
        if (
            child.is_dir()
            and child.name not in SKIPPED_DIRECTORIES
            and not child.name.startswith(".")
        ):
            _walk(child, base, found)


def _is_vendored(relative: Path, base: Path) -> bool:
    tree, *rest = relative.parts
    if tree != DOMAINS_TREE or len(rest) < MIN_VENDORED_DEPTH:
        return False
    return (base / tree / rest[0] / VENDOR_MARKER).is_file()


def check_skills(root: Path) -> list[Finding]:
    """Check every published skill; an empty result means all pass."""
    findings: list[Finding] = []
    skills = discover(root)
    findings.extend(_check_unique_names(skills))
    for skill in skills:
        file = root / PUBLISHED_ROOT / skill.path / SKILL_FILE
        shown = (PUBLISHED_ROOT / skill.path / SKILL_FILE).as_posix()
        text = file.read_text(encoding="utf-8")
        findings.extend(_check_frontmatter(shown, skill, text))
        findings.extend(
            check_harness_files(root / PUBLISHED_ROOT / skill.path, shown.removesuffix("/SKILL.md"))
        )
        if skill.path.startswith(f"./{DOMAINS_TREE}/") and not skill.vendored:
            findings.extend(_check_tool_names(root / PUBLISHED_ROOT / skill.path, root))
    return findings


def _check_unique_names(skills: list[PublishedSkill]) -> list[Finding]:
    """One finding per skill whose name another skill already uses.

    The ``skills`` CLI keeps only the first skill of a name and Codex lists both, so a
    repeated name is ambiguous in every installer.
    """
    first: dict[str, str] = {}
    findings: list[Finding] = []
    for skill in skills:
        earlier = first.setdefault(skill.folder, skill.path)
        if earlier != skill.path:
            shown = (PUBLISHED_ROOT / skill.path / SKILL_FILE).as_posix()
            findings.append(Finding(shown, f"name `{skill.folder}` is also used by `{earlier}`"))
    return findings


def _check_frontmatter(shown: str, skill: PublishedSkill, text: str) -> list[Finding]:
    try:
        frontmatter = parse_frontmatter(text)
    except FrontmatterError as exc:
        return [Finding(shown, f"unreadable frontmatter: {exc}")]
    findings: list[Finding] = []
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        findings.append(Finding(shown, "frontmatter needs a non-empty `name`"))
    else:
        if name != skill.folder:
            findings.append(
                Finding(shown, f"`name: {name}` must equal its folder name `{skill.folder}`")
            )
        if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.match(name):
            findings.append(
                Finding(
                    shown,
                    f"`name: {name}` must be lowercase letters, digits and hyphens, "
                    f"at most {MAX_NAME_LENGTH} characters",
                )
            )
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        findings.append(Finding(shown, "frontmatter needs a non-empty `description`"))
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        findings.append(
            Finding(
                shown,
                f"`description` is {len(description)} characters; the limit is "
                f"{MAX_DESCRIPTION_LENGTH}",
            )
        )
    if not skill.vendored:
        unknown = sorted(set(frontmatter) - SPEC_FIELDS)
        if unknown:
            findings.append(
                Finding(
                    shown,
                    f"unknown frontmatter field(s) {', '.join(f'`{key}`' for key in unknown)}; "
                    f"the Agent Skills specification allows {', '.join(sorted(SPEC_FIELDS))}",
                )
            )
    findings.extend(_check_optional_fields(shown, frontmatter))
    return findings


def _check_optional_fields(
    shown: str, frontmatter: dict[str, str | dict[str, str]]
) -> list[Finding]:
    findings = [
        Finding(shown, f"`{key}` must be a string")
        for key in STRING_FIELDS
        if key in frontmatter and not isinstance(frontmatter[key], str)
    ]
    compatibility = frontmatter.get("compatibility")
    if isinstance(compatibility, str) and not 1 <= len(compatibility) <= MAX_COMPATIBILITY_LENGTH:
        findings.append(
            Finding(
                shown,
                f"`compatibility` is {len(compatibility)} characters; it must be 1 to "
                f"{MAX_COMPATIBILITY_LENGTH}",
            )
        )
    if "metadata" in frontmatter and not isinstance(frontmatter["metadata"], dict):
        findings.append(Finding(shown, "`metadata` must be a mapping of string keys to strings"))
    return findings


def _check_tool_names(folder: Path, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for file in sorted(folder.rglob("*")):
        if not file.is_file() or file.suffix not in {".md", ".txt", ".toml", ".sh", ".py"}:
            continue
        if any(part in SKIPPED_DIRECTORIES for part in file.relative_to(folder).parts):
            continue
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            findings.extend(
                Finding(
                    f"{file.relative_to(root).as_posix()}:{number}",
                    f"names the VibeSys-only tool `{tool}`; a domain skill also runs in a plain "
                    "Claude Code session, so describe the action without the tool name",
                )
                for tool in VIBESYS_TOOL_NAMES
                if tool in line
            )
    return findings
