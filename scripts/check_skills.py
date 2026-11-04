"""Fail when an agent skill in `.agents/skills/` breaks the skill convention.

The skills in `.agents/skills/` (the `.claude/skills` symlink points at them)
guide agents that develop this repository. They drift faster than code: before
this check existed, skills named deleted modules, a deleted architecture test,
the wrong Hypothesis profile switch and a CI shard count off by a factor of
five, and nothing failed. The convention itself is in the `write-skills` skill;
this script is its enforcement, and each rule below names what it checks.

Rules (the `Rule` enum):
    frontmatter
        `SKILL.md` opens with a `---` block holding exactly `name` (equal to
        the directory name), `description` (containing "Use when"), and
        `metadata` with exactly `kind`, one of `SkillKind`. Any other key is
        rejected by name. The block is the YAML subset `parse_frontmatter`
        reads: `key: value`, a folded or literal block scalar, and one nested
        mapping; anything else is an error, not a guess.
    skill-lines, reference-lines
        `SKILL.md` has at most `MAX_SKILL_LINES` lines and each
        `references/*.md` at most `MAX_REFERENCE_LINES`.
    layout
        A skill directory holds only `ALLOWED_ENTRIES`, and `references/` is
        one level deep.
    unlinked-reference
        Every file in `references/` is linked from `SKILL.md`.
    unrun-script
        Every file in `scripts/` is named (by its repo path) in a tracked file
        under `RUNNER_ROOTS`, so a test or CI runs it.
    unresolved-reference
        Every `path:symbol` citation, dotted package name and repo path in a
        skill's Markdown resolves (`scripts/check_doc_citations.py`,
        `check_references`). A path may also be relative to the skill directory.
    repo-specific-name
        A `generic` skill names nothing of this repository: no tracked
        top-level directory as a path prefix, no `vibesys`, no `vs_`/`vs-`
        package, no `REPO_FILES`. See `repo_specific_pattern`.
    cites-codebase-skill
        A `generic` skill does not name or link a `codebase` skill.
    codebase-sections
        A `codebase` skill's `##` sections are exactly `CODEBASE_SECTIONS`, in order.
    rule-without-enforcement
        In a `codebase` skill, every top-level list item under
        `ENFORCED_SECTIONS` carries `Unenforced:` or cites, in a code span, a
        check that exists by its identifier (`CheckIndex.cites_check` in
        `scripts/skill_tokens.py`): a test name, a check script name, a
        package script, an enabled lint rule, or a stable command. Never by
        its file: the check's own failure message carries the location.
    path-token
        No skill Markdown, and not the index, names a repository path, a
        repository file, a module path past a package's `api` facade
        (`scripts/skill_tokens.py`, `path_tokens`).
    issue-number
        No skill Markdown names an issue or pull request number.
    unresolved-check
        Every code span shaped like a check citation (`test_<name>`,
        `check_<name>`, `pnpm <scope>:<name>`, `lint/<group>/<rule>`,
        `repoctl run-checks --group <name>`) names a
        check that exists, wherever it appears in a skill.
    index
        `.agents/skills/README.md` lists every skill exactly once, under the
        `##` heading of its kind, as a `[name](name/SKILL.md)` link, and lists
        nothing else.

Existing violations:
    `scripts/skill_violations.json` records each `(path, rule)` that still
    fails, with its count (for the line rules, the line count) and a reason.
    It is a shrink-only ratchet: a count above the record fails, and a count
    below it fails until the record is lowered to match, so a fixed violation
    cannot be silently replaced by a new one. Never add an entry for a new
    violation; fix it.

Usage:
    uv run python -m scripts.check_skills [--survey]  (Python 3.12+; CI runs it with `python3`).

`--survey` prints every violation, allowlisted or not. Stdlib only, like the
doc checks it builds on, so it runs in CI's `doc-checks` job, which every pull
request runs; the Python job skips a change that touches only `.agents/`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from scripts.check_doc_citations import (
    RepoIndex,
    check_references,
    repo_index,
    tracked_paths,
)
from scripts.check_doc_links import extract_links, iter_code_spans, run_git
from scripts.skill_tokens import CheckIndex, PathIndex, TokenKind, describe, path_tokens

SKILLS_DIR = ".agents/skills"
INDEX = f"{SKILLS_DIR}/README.md"
ALLOWLIST = "scripts/skill_violations.json"
MAX_SKILL_LINES = 150
MAX_REFERENCE_LINES = 300
# Path segments of a file directly in `references/`: the directory and the file name.
REFERENCE_DEPTH = 2
ALLOWED_ENTRIES = frozenset({"SKILL.md", "references", "scripts", "agents", "assets"})
FRONTMATTER_KEYS = ("name", "description", "metadata")
METADATA_KEYS = ("kind",)
TRIGGER_PHRASE = "Use when"
CODEBASE_SECTIONS = (
    "Builds on",
    "When this applies",
    "Steps",
    "Testing",
    "Known failure modes",
    "Sources of truth",
)
ENFORCED_SECTIONS = frozenset({"Steps", "Testing", "Known failure modes"})
UNENFORCED_MARK = "Unenforced:"
RUNNER_ROOTS = ("tests/", ".github/", ".repoctl/")
# Root files that name this repository's own setup rather than a language idiom.
REPO_FILES = ("AGENTS.md", "CLAUDE.md", "tach.toml")

LIST_ITEM = re.compile(r"^(?:[-*]|\d+\.)\s")
HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
INDEX_ENTRY = re.compile(r"\]\((?P<name>[\w.-]+)/SKILL\.md\)")
FRONTMATTER_LINE = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):(?:\s+(?P<value>.*))?$")
BLOCK_SCALAR = frozenset({">", ">-", "|", "|-"})

MIN_PYTHON = (3, 12)

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2


class SkillKind(StrEnum):
    """The three kinds of skill; `write-skills` says what each may contain."""

    GENERIC = "generic"
    CODEBASE = "codebase"
    WORKFLOW = "workflow"


class Rule(StrEnum):
    """The rules this script checks, named in output and in the allowlist."""

    FRONTMATTER = "frontmatter"
    SKILL_LINES = "skill-lines"
    REFERENCE_LINES = "reference-lines"
    LAYOUT = "layout"
    UNLINKED_REFERENCE = "unlinked-reference"
    UNRUN_SCRIPT = "unrun-script"
    UNRESOLVED_REFERENCE = "unresolved-reference"
    REPO_SPECIFIC_NAME = "repo-specific-name"
    CITES_CODEBASE_SKILL = "cites-codebase-skill"
    CODEBASE_SECTIONS = "codebase-sections"
    RULE_WITHOUT_ENFORCEMENT = "rule-without-enforcement"
    REPO_PATH = "path-token"
    ISSUE_NUMBER = "issue-number"
    UNRESOLVED_CHECK = "unresolved-check"
    INDEX = "index"


class FrontmatterError(ValueError):
    """`SKILL.md` frontmatter that is malformed or breaks the schema; names the line or key."""


class AllowlistError(ValueError):
    """An allowlist file that does not match its schema; names the offending entry."""


@dataclass(frozen=True)
class Violation:
    """One broken rule at ``path``:``line``; ``count`` is its weight in the ratchet."""

    path: str
    rule: Rule
    line: int
    message: str
    count: int = 1


@dataclass(frozen=True)
class Frontmatter:
    """A validated `SKILL.md` frontmatter block."""

    name: str
    description: str
    kind: SkillKind


@dataclass(frozen=True)
class Skill:
    """One skill directory: its name and its tracked files, relative to it."""

    name: str
    files: tuple[str, ...]

    @property
    def directory(self) -> str:
        """The skill's repo-relative directory."""
        return f"{SKILLS_DIR}/{self.name}"


@dataclass(frozen=True)
class AllowlistEntry:
    """A recorded violation count for one ``(path, rule)``, and why it remains."""

    path: str
    rule: Rule
    count: int
    reason: str


def check_index(index: RepoIndex) -> CheckIndex:
    """The citable checks under ``index``: tests, check scripts, package scripts and lint rules."""
    pyproject = index.root / "pyproject.toml"
    lint: dict[str, list[str]] = {}
    if pyproject.is_file():
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        lint = config.get("tool", {}).get("ruff", {}).get("lint", {})
    biome = [p for p in index.tracked if p.startswith("clients/") and "/biome" in f"/{p}"]
    biome_text = "\n".join((index.root / p).read_text(encoding="utf-8") for p in biome)
    select = tuple(lint.get("select", ())) + tuple(lint.get("extend-select", ()))
    return CheckIndex(index, select, frozenset(lint.get("ignore", ())), biome_text)


def _scalar(value: str) -> str:
    """A plain or quoted YAML scalar's text."""
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, str | dict[str, str]]:
    """Read the frontmatter block of a `SKILL.md`, in the YAML subset skills use.

    Raises:
        FrontmatterError: If the block is missing, unterminated, or uses YAML
            this subset does not read; the message names the line.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        message = "line 1: SKILL.md must open with a `---` frontmatter line"
        raise FrontmatterError(message)
    try:
        end = lines.index("---", 1)
    except ValueError:
        message = "the frontmatter block has no closing `---` line"
        raise FrontmatterError(message) from None
    result: dict[str, str | dict[str, str]] = {}
    position = 1
    while position < end:
        position = _parse_entry(lines, position, end, result)
    return result


def _parse_entry(
    lines: list[str], position: int, end: int, into: dict[str, str | dict[str, str]]
) -> int:
    """Parse the top-level entry at ``position`` into ``into``; return the next position."""
    match = FRONTMATTER_LINE.match(lines[position])
    if match is None:
        message = f"line {position + 1}: expected `key: value`, got {lines[position]!r}"
        raise FrontmatterError(message)
    key, value = match.group("key"), (match.group("value") or "").strip()
    if key in into:
        message = f"line {position + 1}: duplicate key `{key}`"
        raise FrontmatterError(message)
    body: list[str] = []
    following = position + 1
    while following < end and (lines[following].startswith((" ", "\t")) or not lines[following]):
        body.append(lines[following].strip())
        following += 1
    if value in BLOCK_SCALAR:
        into[key] = " ".join(part for part in body if part)
    elif value:
        if any(body):
            message = f"line {position + 2}: unexpected indented line after `{key}`"
            raise FrontmatterError(message)
        into[key] = _scalar(value)
    else:
        into[key] = _parse_mapping(body, position + 2, key)
    return following


def _parse_mapping(body: list[str], first_line: int, owner: str) -> dict[str, str]:
    """Parse the indented ``key: value`` lines nested under ``owner``."""
    mapping: dict[str, str] = {}
    for offset, line in enumerate(body):
        if not line:
            continue
        match = FRONTMATTER_LINE.match(line)
        if match is None or not match.group("value"):
            message = f"line {first_line + offset}: expected `key: value` under `{owner}`"
            raise FrontmatterError(message)
        mapping[match.group("key")] = _scalar(match.group("value"))
    return mapping


def validate_frontmatter(fields: dict[str, str | dict[str, str]], directory: str) -> Frontmatter:
    """Check the parsed frontmatter of the skill in ``directory`` against the schema.

    Raises:
        FrontmatterError: Naming the first unknown, missing or invalid key.
    """
    for key in fields:
        if key not in FRONTMATTER_KEYS:
            allowed = ", ".join(FRONTMATTER_KEYS)
            message = f"unknown key `{key}`; a skill's frontmatter holds only {allowed}"
            raise FrontmatterError(message)
    name, description, metadata = (fields.get(key) for key in FRONTMATTER_KEYS)
    if not isinstance(name, str) or name != directory:
        message = f"`name` must be the directory name `{directory}`, got {name!r}"
        raise FrontmatterError(message)
    if not isinstance(description, str) or TRIGGER_PHRASE not in description:
        message = f"`description` must say what the skill does, then `{TRIGGER_PHRASE} ...`"
        raise FrontmatterError(message)
    if not isinstance(metadata, dict):
        message = "`metadata` must be a mapping holding `kind`"
        raise FrontmatterError(message)
    for key in metadata:
        if key not in METADATA_KEYS:
            message = f"unknown key `metadata.{key}`; `metadata` holds only `kind`"
            raise FrontmatterError(message)
    kinds = ", ".join(kind.value for kind in SkillKind)
    try:
        kind = SkillKind(metadata.get("kind", ""))
    except ValueError:
        message = f"`metadata.kind` must be one of {kinds}"
        raise FrontmatterError(message) from None
    return Frontmatter(name, description, kind)


def discover_skills(tracked: tuple[str, ...]) -> list[Skill]:
    """Every skill directory under `SKILLS_DIR`, with its tracked files."""
    files: dict[str, list[str]] = {}
    prefix = f"{SKILLS_DIR}/"
    for path in tracked:
        if path.startswith(prefix) and "/" in path.removeprefix(prefix):
            name, rest = path.removeprefix(prefix).split("/", 1)
            files.setdefault(name, []).append(rest)
    return [Skill(name, tuple(sorted(paths))) for name, paths in sorted(files.items())]


def _line_count(path: Path) -> int:
    """Physical lines in ``path``, as `wc -l` counts them."""
    return len(path.read_text(encoding="utf-8").splitlines())


def layout_violations(skill: Skill, root: Path) -> list[Violation]:
    """Unexpected entries, nested references, oversized files, and unlinked references."""
    found: list[Violation] = []
    for relative in skill.files:
        path = f"{skill.directory}/{relative}"
        parts = relative.split("/")
        if parts[0] not in ALLOWED_ENTRIES:
            allowed = ", ".join(sorted(ALLOWED_ENTRIES))
            found.append(Violation(path, Rule.LAYOUT, 1, f"a skill holds only {allowed}"))
        elif parts[0] == "references" and len(parts) > REFERENCE_DEPTH:
            found.append(Violation(path, Rule.LAYOUT, 1, "references/ is one level deep"))
        elif parts[0] == "references" and relative.endswith(".md"):
            lines = _line_count(root / path)
            if lines > MAX_REFERENCE_LINES:
                message = f"{lines} lines; a reference holds at most {MAX_REFERENCE_LINES}"
                found.append(Violation(path, Rule.REFERENCE_LINES, 1, message, lines))
    skill_md = f"{skill.directory}/SKILL.md"
    if "SKILL.md" not in skill.files:
        return [*found, Violation(skill_md, Rule.LAYOUT, 1, "a skill directory needs a SKILL.md")]
    lines = _line_count(root / skill_md)
    if lines > MAX_SKILL_LINES:
        message = (
            f"{lines} lines; SKILL.md holds at most {MAX_SKILL_LINES}: move depth to references/"
        )
        found.append(Violation(skill_md, Rule.SKILL_LINES, 1, message, lines))
    linked = {_link_path(link.target) for link in extract_links(root / skill_md)}
    for relative in skill.files:
        if relative.startswith("references/") and relative not in linked:
            message = f"`{relative}` is not linked from SKILL.md: link it, saying when to open it"
            found.append(Violation(skill_md, Rule.UNLINKED_REFERENCE, 1, message))
    return found


def _link_path(target: str) -> str:
    """A link target without its anchor or leading `./`."""
    return target.split("#", 1)[0].removeprefix("./")


def script_violations(skill: Skill, runner_text: str) -> list[Violation]:
    """Files in `scripts/` that no test, workflow or check group names."""
    found: list[Violation] = []
    for relative in skill.files:
        if relative.startswith("scripts/") and f"{skill.directory}/{relative}" not in runner_text:
            path = f"{skill.directory}/{relative}"
            message = "no test or CI step runs this script: run it from one, or make it a reference"
            found.append(Violation(path, Rule.UNRUN_SCRIPT, 1, message))
    return found


def runner_text(root: Path, tracked: tuple[str, ...]) -> str:
    """The text of every tracked file under `RUNNER_ROOTS`, where scripts are run from."""
    texts: list[str] = []
    for path in tracked:
        if path.startswith(RUNNER_ROOTS):
            try:
                texts.append((root / path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(texts)


def repo_specific_pattern(index: RepoIndex) -> re.Pattern[str]:
    """What a generic skill must not contain: names of this repository's own layout.

    A path into a tracked top-level directory (`src/`, `tests/`, `.github/`),
    the project name in any case, a `vs_`/`vs-` package name, or one of `REPO_FILES`.
    """
    directories = "|".join(re.escape(name) for name in sorted(index.top_dirs))
    files = "|".join(re.escape(name) for name in REPO_FILES)
    return re.compile(
        rf"(?<![\w.-])(?:{directories})/|(?i:vibesys)|\bvs[_-][a-z][\w-]*|(?<![\w.-])(?:{files})\b"
    )


def generic_violations(
    skill: Skill, root: Path, pattern: re.Pattern[str], codebase_skills: frozenset[str]
) -> list[Violation]:
    """Repo-specific names in a generic skill, and names of codebase skills it relies on."""
    found: list[Violation] = []
    for relative in skill.files:
        path = f"{skill.directory}/{relative}"
        try:
            text = (root / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                message = f"`{match.group(0)}` names this repository; a generic skill must not"
                found.append(Violation(path, Rule.REPO_SPECIFIC_NAME, lineno, message))
            for name in codebase_skills:
                if f"`{name}`" in line or f"../{name}/" in line:
                    message = f"names the codebase skill `{name}`; generic skills cite none"
                    found.append(Violation(path, Rule.CITES_CODEBASE_SKILL, lineno, message))
    return found


def sections(text: str) -> list[tuple[str, int, list[str]]]:
    """The `##` sections of ``text``: title, 1-based heading line, and body lines."""
    found: list[tuple[str, int, list[str]]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = HEADING.match(line)
        if heading is not None:
            found.append((heading.group("title"), lineno, []))
        elif found:
            found[-1][2].append(line)
    return found


def list_items(body: list[str], first_line: int) -> list[tuple[int, str]]:
    """Top-level list items in ``body``, each with its line and full text."""
    items: list[tuple[int, list[str]]] = []
    for offset, line in enumerate(body):
        if LIST_ITEM.match(line):
            items.append((first_line + offset, [line]))
        elif items and (line.startswith((" ", "\t")) or (line and not line.startswith("#"))):
            items[-1][1].append(line)
    return [(lineno, "\n".join(lines)) for lineno, lines in items]


def token_violations(path: str, text: str, paths: PathIndex, checks: CheckIndex) -> list[Violation]:
    """Path tokens, issue numbers and citations of checks that do not exist, in one Markdown file."""
    found: list[Violation] = []
    for token in path_tokens(text, path, paths):
        rule = Rule.ISSUE_NUMBER if token.kind is TokenKind.ISSUE else Rule.REPO_PATH
        found.append(Violation(path, rule, token.line, describe(token)))
    for span in iter_code_spans(text):
        reason = checks.unresolved_check(span.text)
        if reason is not None:
            message = f"{reason}: cite a check that exists, by its identifier, or drop the citation"
            found.append(Violation(path, Rule.UNRESOLVED_CHECK, span.line, message))
    return found


def codebase_violations(skill: Skill, root: Path, checks: CheckIndex) -> list[Violation]:
    """Section order, and list items in rule sections with no enforcement or `Unenforced:`."""
    path = f"{skill.directory}/SKILL.md"
    found_sections = sections((root / path).read_text(encoding="utf-8"))
    titles = tuple(title for title, _, _ in found_sections)
    found: list[Violation] = []
    if titles != CODEBASE_SECTIONS:
        expected = ", ".join(CODEBASE_SECTIONS)
        message = (
            f"sections are {', '.join(titles) or 'none'}; a codebase skill has exactly: {expected}"
        )
        found.append(Violation(path, Rule.CODEBASE_SECTIONS, 1, message))
    for title, heading_line, body in found_sections:
        if title not in ENFORCED_SECTIONS:
            continue
        for lineno, item in list_items(body, heading_line + 1):
            spans = [span.text for span in iter_code_spans(item)]
            if UNENFORCED_MARK in item or any(checks.cites_check(span) for span in spans):
                continue
            message = (
                f"this `{title}` item names no check that exists; cite the check that enforces it "
                f"by its identifier (a test name, check script name, `pnpm` script, lint rule ID or "
                f"stable command), or write `{UNENFORCED_MARK} <reason>`"
            )
            found.append(Violation(path, Rule.RULE_WITHOUT_ENFORCEMENT, lineno, message))
    return found


def index_violations(root: Path, kinds: dict[str, SkillKind]) -> list[Violation]:
    """Skills missing from the index, listed twice, under the wrong kind, or not existing."""
    if not (root / INDEX).is_file():
        return [Violation(INDEX, Rule.INDEX, 1, "the skills index is missing")]
    listed: Counter[tuple[str, str]] = Counter()
    lines: dict[str, int] = {}
    for title, heading_line, body in sections((root / INDEX).read_text(encoding="utf-8")):
        for offset, line in enumerate(body, start=1):
            for match in INDEX_ENTRY.finditer(line):
                listed[(title, match.group("name"))] += 1
                lines.setdefault(match.group("name"), heading_line + offset)
    found: list[Violation] = []
    for name, kind in sorted(kinds.items()):
        heading = kind.value.capitalize()
        if listed[(heading, name)] != 1:
            message = (
                f"list `{name}` exactly once under `## {heading}`, as `[{name}]({name}/SKILL.md)`"
            )
            found.append(Violation(INDEX, Rule.INDEX, lines.get(name, 1), message))
    for title, name in sorted(listed):
        if name not in kinds:
            message = f"lists `{name}` under `## {title}`, but no skill has that name"
            found.append(Violation(INDEX, Rule.INDEX, lines[name], message))
    return found


def frontmatter_of(skill: Skill, root: Path) -> Frontmatter | Violation:
    """The skill's validated frontmatter, or the violation explaining why it is invalid."""
    path = f"{skill.directory}/SKILL.md"
    try:
        fields = parse_frontmatter((root / path).read_text(encoding="utf-8"))
        return validate_frontmatter(fields, skill.name)
    except FrontmatterError as exc:
        return Violation(path, Rule.FRONTMATTER, 1, str(exc))
    except FileNotFoundError:
        return Violation(path, Rule.FRONTMATTER, 1, "SKILL.md is missing")


def check_tree(root: Path, tracked: tuple[str, ...]) -> list[Violation]:
    """Every violation of the skill convention in the skills under ``root``."""
    index = repo_index(root, tracked)
    checks = check_index(index)
    paths = PathIndex(index)
    skills = discover_skills(tracked)
    kinds: dict[str, SkillKind] = {}
    found: list[Violation] = []
    for skill in skills:
        frontmatter = frontmatter_of(skill, root)
        if isinstance(frontmatter, Violation):
            found.append(frontmatter)
        else:
            kinds[skill.name] = frontmatter.kind
    runners = (
        runner_text(root, tracked) if any("scripts/" in f for s in skills for f in s.files) else ""
    )
    pattern = repo_specific_pattern(index)
    codebase = frozenset(name for name, kind in kinds.items() if kind is SkillKind.CODEBASE)
    for skill in skills:
        found += layout_violations(skill, root)
        found += script_violations(skill, runners)
        markdown = [root / skill.directory / f for f in skill.files if f.endswith(".md")]
        for file in markdown:
            relative = file.relative_to(root).as_posix()
            found += token_violations(relative, file.read_text(encoding="utf-8"), paths, checks)
        for finding in check_references(markdown, index, base=skill.directory):
            path = finding.source.relative_to(root).as_posix()
            message = f"`{finding.text}`: {finding.reason}"
            found.append(Violation(path, Rule.UNRESOLVED_REFERENCE, finding.line, message))
        if kinds.get(skill.name) is SkillKind.GENERIC:
            found += generic_violations(skill, root, pattern, codebase)
        elif kinds.get(skill.name) is SkillKind.CODEBASE:
            found += codebase_violations(skill, root, checks)
    if (root / INDEX).is_file():
        found += token_violations(INDEX, (root / INDEX).read_text(encoding="utf-8"), paths, checks)
    return found + index_violations(root, kinds)


def load_allowlist(text: str) -> list[AllowlistEntry]:
    """Parse the allowlist JSON, rejecting unknown keys and rules by name.

    Raises:
        AllowlistError: Naming the entry and key that does not match the schema.
    """
    entries: list[AllowlistEntry] = []
    for position, raw in enumerate(json.loads(text)):
        keys = set(raw)
        if keys != {"path", "rule", "count", "reason"}:
            message = (
                f"entry {position}: keys must be path, rule, count, reason; got {sorted(keys)}"
            )
            raise AllowlistError(message)
        try:
            rule = Rule(raw["rule"])
        except ValueError:
            message = f"entry {position}: unknown rule `{raw['rule']}`"
            raise AllowlistError(message) from None
        if not isinstance(raw["count"], int) or raw["count"] < 1 or not str(raw["reason"]).strip():
            message = f"entry {position}: count must be a positive integer and reason non-empty"
            raise AllowlistError(message)
        entries.append(AllowlistEntry(str(raw["path"]), rule, raw["count"], str(raw["reason"])))
    return entries


def ratchet(violations: list[Violation], allowlist: list[AllowlistEntry]) -> list[str]:
    """Compare violations with the allowlist: new or grown ones fail, and so do stale records."""
    counts: Counter[tuple[str, Rule]] = Counter()
    for violation in violations:
        counts[(violation.path, violation.rule)] += violation.count
    recorded = {(entry.path, entry.rule): entry.count for entry in allowlist}
    errors = [
        f"{violation.path}:{violation.line}: [{violation.rule}] {violation.message}"
        for violation in violations
        if (violation.path, violation.rule) not in recorded
    ]
    for (path, rule), count in sorted(recorded.items()):
        current = counts.get((path, rule), 0)
        if current > count:
            errors.append(
                f"{path}: [{rule}] grew from {count} to {current}; fix the new violation, "
                f"never raise the record in {ALLOWLIST}"
            )
        elif current < count:
            fix = f"lower its count to {current}" if current else "delete its entry"
            errors.append(
                f"{path}: [{rule}] shrank from {count} to {current}; {fix} in {ALLOWLIST}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    """Check every skill and print what breaks the convention beyond the allowlist."""
    parser = argparse.ArgumentParser(description="Check the agent skills in .agents/skills/.")
    parser.add_argument(
        "--survey", action="store_true", help="Print every violation, allowlisted or not."
    )
    args = parser.parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        # Resolving a dotted name parses the repository's modules, which use 3.12 syntax.
        print(f"check_skills: needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+", file=sys.stderr)
        return EXIT_TOOL_ERROR
    root = Path(run_git(["git", "rev-parse", "--show-toplevel"]).strip())
    violations = check_tree(root, tracked_paths(root))
    if args.survey:
        for violation in violations:
            print(f"{violation.path}:{violation.line}: [{violation.rule}] {violation.message}")
        return EXIT_OK
    try:
        allowlist = load_allowlist((root / ALLOWLIST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"check_skills: cannot read {ALLOWLIST}: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    errors = ratchet(violations, allowlist)
    for error in errors:
        print(error)
    if errors:
        print(
            f"\n{len(errors)} skill convention failure(s); the convention is in the write-skills skill."
        )
        return EXIT_VIOLATIONS
    print(
        f"Checked {len(discover_skills(tracked_paths(root)))} skill(s); every one follows the convention."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
