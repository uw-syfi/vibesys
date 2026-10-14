"""What an agent skill may name: the path-like tokens it must not, and the checks it cites.

A skill that points at files breaks whenever the code moves, and a reader
cannot tell a stale pointer from a live one. So a skill names concepts,
library and package names, public entry points (`vs_agent.api`), stable
commands and the checks that enforce its rules, and leaves locations to the
code and to each check's failure message. `scripts/check_skills.py` reports
what this module finds; the convention itself is the `write-skills` skill.

Path tokens (`path_tokens`):
    Scanned on every line, fenced blocks included, in a skill's Markdown and
    in the skills index. Each finding is one token, of one `TokenKind`:

    path
        A slash-separated path into the repository: its first segment is a
        tracked top-level directory, its segments occur in order in a tracked
        path, or it ends in a file name; and a link or URL to such a path.
        Allowed: a path into the skill's own directory (its references),
        into the skills directory (another skill), to a tracked page under
        `docs/`, or to a root file in `WELL_KNOWN_FILES` (`AGENTS.md`); a
        bare skill layout entry (`references/`); an npm scope package
        (`@vibesys/run-session`); and `STABLE_COMMANDS`.
    file
        A bare file name that a tracked file has, such as `test_loop.py` or
        `loop.py:run`, except `WELL_KNOWN_FILES`, which name a tool's
        convention rather than this repository's layout, and the names of
        skill files and docs pages, which skills may link.
    module
        A dotted name into one of the repository's packages that does not go
        through the package's public facade: `vs_agent.api.Session` passes,
        `vs_agent.providers.claude` does not.
    issue
        An issue or pull request number (`#123`, a GitHub issue or pull URL).
        History belongs in the issue tracker and in Git, not in a skill.

Check citations (`CheckIndex`):
    A check is cited by a stable identifier in an inline code span, never by
    its file. The span is entirely one of:

    - a Python test function, `test_<name>`, defined in a tracked test file;
    - a check script, `check_<name>`, the stem of a tracked `check_*` file
      under `scripts/` or `clients/tools/`;
    - a client package script with a namespaced name, `pnpm check:knip`;
    - an enabled Ruff code (`TID251`) or Biome rule (`lint/style/noVar`);
    - a Grit lint rule (`ui-no-timers`) or a dependency-cruiser rule
      (`no-circular`), by the name its configuration gives it;
    - a CI check group defined in `.repoctl/checks.toml`, by its name
      (`python_guards`) or as the command that runs it
      (`./support/repoctl/repoctl run-checks --group python_guards`);
    - one of `STABLE_COMMANDS`, alone or with arguments.

    `unresolved_check` names a span shaped like the first three kinds that
    names nothing, wherever it appears in a skill, so a cited check cannot
    be renamed or deleted without the skill failing.

Stdlib only, like the checks it serves.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING

from scripts.check_doc_citations import DOTTED_NAME, FILE_SUFFIXES

if TYPE_CHECKING:
    from scripts.check_doc_citations import RepoIndex

SKILLS_ROOTS = (".agents/skills", ".claude/skills")
DOCS_ROOT = "docs"
# The commands every change runs. A skill may name them as commands and as checks.
STABLE_COMMANDS = frozenset(
    {
        "./support/repoctl/repoctl",
        "./scripts/check_format.sh",
        "./scripts/check_lint.sh",
        "./scripts/check_types.sh",
    }
)
# File names that a tool, a convention or a public product contract (a task bundle,
# an evaluator package, the run input) fixes, not this repository's layout.
WELL_KNOWN_FILES = frozenset(
    {
        "AGENTS.md",
        "CANDIDATE_CONTRACT.md",
        "Cargo.toml",
        "OBJECTIVE.md",
        "go.mod",
        "vibesys.evaluator.toml",
        "vibesys.input.toml",
        "CLAUDE.md",
        "README.md",
        "SKILL.md",
        "__init__.py",
        "conftest.py",
        "devcontainer-feature.json",
        "package.json",
        "pyproject.toml",
        "tach.toml",
    }
)
# Entries of a skill directory, named when a skill describes skill layout.
SKILL_LAYOUT = frozenset({"references", "scripts", "agents", "assets"})
FACADE = "api"

URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s)>\]`'\"]*")
REPO_URL = re.compile(r"^https://github\.com/uw-syfi/vibesys/(?:blob|tree)/[^/]+/(?P<path>\S+)$")
ISSUE_URL = re.compile(r"^https://github\.com/[^/]+/[^/]+/(?:issues|pull)/\d+")
ISSUE_NUMBER = re.compile(r"(?<![\w&#/])#\d+\b")
SLASH_TOKEN = re.compile(r"(?<![\w.@/~:-])(?:\.{1,2}/)*@?[\w.*?<>{}-]+(?:/[\w.*?<>{}@-]*)+")
FILE_TOKEN = re.compile(r"(?<![\w./@-])[\w-]+(?:\.[\w-]+)*\.[A-Za-z][\w]*(?![\w/])")
DOTTED_TOKEN = re.compile(r"(?<![\w./-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?![\w/(-])")
FILE_NAME = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")
PLACEHOLDER = re.compile(r"[<>{}*?]")

TEST_NAME = re.compile(r"^test_\w+$")
CHECK_NAME = re.compile(r"^check_\w+$")
PNPM_SPAN = re.compile(r"^pnpm\s+(?P<args>.+)$")
PNPM_SCRIPT = re.compile(r"^[\w-]+:[\w:.-]+$")
TEST_DEFINITION = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.MULTILINE)
CHECK_FILE = re.compile(r"(?:^|/)(?P<stem>check_\w+?)(?:\.test)?\.(?:py|sh|mjs|cjs|js|ts)$")
DEPCRUISE_NAME = re.compile(r"""\bname:\s*["'](?P<name>[\w-]+)["']""")
RUFF_CODE = re.compile(r"^[A-Z]{1,4}[0-9]{1,4}$")
BIOME_RULE = re.compile(r"^(?:lint/)?[a-z]+/(?P<rule>[a-z][A-Za-z]+)$")
PYTHON_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]+_test|conftest)\.py$")
CHECK_ROOTS = ("scripts/", "clients/tools/")
DEPCRUISE_CONFIG = "clients/.dependency-cruiser.mjs"
CHECK_GROUPS_CONFIG = ".repoctl/checks.toml"
GROUP_SPAN = re.compile(
    r"^(?:\./support/repoctl/)?repoctl\s+run-checks\s+--group\s+(?P<group>[\w-]+)$"
)


class TokenKind(StrEnum):
    """What a forbidden token in a skill names."""

    PATH = "path"
    FILE = "file"
    MODULE = "module"
    ISSUE = "issue"


@dataclass(frozen=True)
class Token:
    """One forbidden token at 1-based ``line`` of a skill file."""

    line: int
    text: str
    kind: TokenKind


ADVICE = {
    TokenKind.PATH: "is a repository path",
    TokenKind.FILE: "names a repository file",
    TokenKind.MODULE: "is a module path that bypasses the package's `api` facade",
    TokenKind.ISSUE: "is an issue or pull request number",
}
FIX = {
    TokenKind.PATH: (
        "name the concept, package or public entry point instead, and cite a check by "
        "its identifier; link only to docs/ pages and other skills"
    ),
    TokenKind.FILE: "name what the file is for, or the check it holds by its identifier",
    TokenKind.MODULE: "name the package, or its public `<package>.api` entry point",
    TokenKind.ISSUE: "state the lesson itself; history lives in the tracker and Git",
}


def describe(token: Token) -> str:
    """A failure message that says what is wrong with ``token`` and how to fix it."""
    return f"`{token.text}` {ADVICE[token.kind]}; {FIX[token.kind]} (write-skills, Content)"


@dataclass(frozen=True)
class PathIndex:
    """The tracked tree, indexed for recognizing path-like tokens."""

    repo: RepoIndex
    joined: str = field(init=False)
    basenames: frozenset[str] = field(init=False)
    # File names a skill may write bare: `WELL_KNOWN_FILES`, skill files, and docs pages.
    citable_names: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        """Index every tracked path and directory as `/segment/.../` text, and every file name."""
        joined = "\n".join(f"/{path}" for path in self.repo.tracked)
        object.__setattr__(self, "joined", joined.replace("\n", "/\n") + "/")
        names = frozenset(path.rsplit("/", 1)[-1] for path in self.repo.tracked)
        object.__setattr__(self, "basenames", names)
        prefixes = (*(f"{root}/" for root in SKILLS_ROOTS), f"{DOCS_ROOT}/")
        pages = {p.rsplit("/", 1)[-1] for p in self.repo.tracked if p.startswith(prefixes)}
        object.__setattr__(self, "citable_names", WELL_KNOWN_FILES | pages)

    def occurs(self, segments: str) -> bool:
        """Whether the ``a/b`` segment sequence occurs, segment-aligned, in a tracked path."""
        return f"/{segments}/" in self.joined

    def is_tracked_dir_or_file(self, path: str) -> bool:
        """Whether ``path`` is a tracked file or a directory holding one."""
        path = path.rstrip("/")
        return path in self.repo.tracked or any(t.startswith(f"{path}/") for t in self.repo.tracked)


def path_tokens(text: str, source: str, index: PathIndex) -> list[Token]:
    """Every forbidden token in ``text``, the content of the skill file at repo path ``source``."""
    skill = _skill_directory(source)
    found: list[Token] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        rest = line
        for url in URL.finditer(line):
            kind = _url_kind(url.group(0), index)
            if kind is not None:
                found.append(Token(lineno, url.group(0), kind))
            rest = rest.replace(url.group(0), " ")
        found += [Token(lineno, m.group(0), TokenKind.ISSUE) for m in ISSUE_NUMBER.finditer(rest)]
        for match in SLASH_TOKEN.finditer(rest):
            token = match.group(0).rstrip(".")
            if _is_forbidden_path(token, source, skill, index):
                found.append(Token(lineno, token, TokenKind.PATH))
        rest = SLASH_TOKEN.sub(" ", rest)
        for match in FILE_TOKEN.finditer(rest):
            name = match.group(0).split(":", 1)[0]
            if name in index.basenames and name not in index.citable_names:
                found.append(Token(lineno, match.group(0), TokenKind.FILE))
        for match in DOTTED_TOKEN.finditer(rest):
            if _bypasses_facade(match.group(0), index.repo):
                found.append(Token(lineno, match.group(0), TokenKind.MODULE))
    return found


def _skill_directory(source: str) -> str:
    """The skill directory holding ``source``, or the skills root for the index."""
    for root in SKILLS_ROOTS:
        if source.startswith(f"{root}/"):
            name = source.removeprefix(f"{root}/").split("/", 1)[0]
            return f"{root}/{name}" if "/" in source.removeprefix(f"{root}/") else root
    return posixpath.dirname(source)


def _url_kind(url: str, index: PathIndex) -> TokenKind | None:
    """The kind of forbidden token ``url`` is, if any: a repo file URL or an issue URL."""
    if ISSUE_URL.match(url):
        return TokenKind.ISSUE
    repo = REPO_URL.match(url)
    if repo is None:
        return None
    path = repo.group("path").split("#", 1)[0]
    is_doc = path.startswith(f"{DOCS_ROOT}/") and index.is_tracked_dir_or_file(path)
    return None if is_doc else TokenKind.PATH


def _is_forbidden_path(token: str, source: str, skill: str, index: PathIndex) -> bool:
    """Whether the slash-separated ``token`` points into the repository outside what skills may name."""
    core = re.sub(r"^(?:\.{1,2}/)+", "", token)
    segments = [part for part in core.split("/") if part]
    if not segments or _is_exempt(token, source, skill, index):
        return False
    if segments[0] in index.repo.top_dirs:
        return True
    if FILE_NAME.search(segments[-1]) and not PLACEHOLDER.search(segments[-1]):
        return segments[-1].split(":", 1)[0] in index.basenames
    return not PLACEHOLDER.search(core) and len(segments) > 1 and index.occurs("/".join(segments))


def _is_exempt(token: str, source: str, skill: str, index: PathIndex) -> bool:
    """Whether ``token`` is a name skills may write: a package, a command, skill layout, or an allowed target."""
    core = re.sub(r"^(?:\.{1,2}/)+", "", token)
    segments = [part for part in core.split("/") if part]
    if token.startswith("@") or token in STABLE_COMMANDS or f"./{token}" in STABLE_COMMANDS:
        return True
    if segments[0] in SKILL_LAYOUT and (len(segments) == 1 or segments[0] != "scripts"):
        return True
    target = token.split("#", 1)[0]
    if token.startswith("."):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
        return _is_allowed_target(resolved, skill, index)
    return _is_allowed_target(core.rstrip("/"), skill, index) or index.is_tracked_dir_or_file(
        f"{skill}/{target}"
    )


def _is_allowed_target(path: str, skill: str, index: PathIndex) -> bool:
    """Whether ``path`` is in the skills tree or this skill, a docs page, or a root `WELL_KNOWN_FILES` file."""
    if path == skill or path.startswith(f"{skill}/"):
        return True
    if any(path == root or path.startswith(f"{root}/") for root in SKILLS_ROOTS):
        return True
    if path in WELL_KNOWN_FILES and path in index.repo.tracked:
        return True
    is_docs = path == DOCS_ROOT or path.startswith(f"{DOCS_ROOT}/")
    return is_docs and index.is_tracked_dir_or_file(path)


def _bypasses_facade(name: str, repo: RepoIndex) -> bool:
    """Whether the dotted ``name`` is a module path into a repository package past its facade."""
    if not DOTTED_NAME.match(name):
        return False
    parts = name.split(".")
    return parts[0] in repo.packages and parts[-1] not in FILE_SUFFIXES and parts[1] != FACADE


@dataclass(frozen=True)
class CheckIndex:
    """The checks a skill can cite by identifier, read lazily from the tracked tree."""

    repo: RepoIndex
    ruff_select: tuple[str, ...]
    ruff_ignore: frozenset[str]
    biome_text: str

    @cached_property
    def test_names(self) -> frozenset[str]:
        """Every Python test function defined in a tracked test file."""
        names: set[str] = set()
        for path in self.repo.tracked:
            if PYTHON_TEST_FILE.search(path) or (
                path.endswith(".py") and "tests" in path.split("/")
            ):
                names.update(TEST_DEFINITION.findall(self._read(path)))
        return frozenset(names)

    @cached_property
    def check_names(self) -> frozenset[str]:
        """The stem of every tracked check script."""
        stems = (CHECK_FILE.search(p) for p in self.repo.tracked if p.startswith(CHECK_ROOTS))
        return frozenset(match.group("stem") for match in stems if match is not None)

    @cached_property
    def package_scripts(self) -> frozenset[str]:
        """Every script name of every tracked `package.json`."""
        names: set[str] = set()
        for path in self.repo.tracked:
            if path.rsplit("/", 1)[-1] == "package.json":
                try:
                    scripts = json.loads(self._read(path)).get("scripts", {})
                except (ValueError, AttributeError):
                    continue
                names.update(scripts if isinstance(scripts, dict) else ())
        return frozenset(names)

    @cached_property
    def lint_rule_names(self) -> frozenset[str]:
        """Named lint rules: Grit rule files by stem, and dependency-cruiser rules by name."""
        grit = {
            p.rsplit("/", 1)[-1].removesuffix(".grit")
            for p in self.repo.tracked
            if p.endswith(".grit")
        }
        cruise = set(DEPCRUISE_NAME.findall(self._read(DEPCRUISE_CONFIG)))
        return frozenset(grit | cruise)

    @cached_property
    def check_groups(self) -> frozenset[str]:
        """The name of every CI check group."""
        try:
            config = tomllib.loads(self._read(CHECK_GROUPS_CONFIG))
        except tomllib.TOMLDecodeError:
            return frozenset()
        groups = config.get("check_groups", [])
        return frozenset(g["name"] for g in groups if isinstance(g, dict) and "name" in g)

    def _read(self, path: str) -> str:
        """The text of the tracked ``path``, or empty when it cannot be read as text."""
        try:
            return (self.repo.root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def enables_lint(self, code: str) -> bool:
        """Whether ``code`` names a Ruff code or Biome rule the configuration enables."""
        if RUFF_CODE.match(code):
            return code not in self.ruff_ignore and any(
                code.startswith(p) for p in self.ruff_select
            )
        biome = BIOME_RULE.match(code)
        return biome is not None and f'"{biome.group("rule")}"' in self.biome_text

    def cites_check(self, span: str) -> bool:
        """Whether a code span cites a check that exists."""
        group = _group(span)
        if group:
            return group in self.check_groups
        words = span.split()
        return (
            (bool(words) and words[0] in STABLE_COMMANDS)
            or span in self.check_groups
            or self.enables_lint(span)
            or span in self.lint_rule_names
            or (TEST_NAME.match(span) is not None and span in self.test_names)
            or (CHECK_NAME.match(span) is not None and span in self.check_names)
            or _pnpm_script(span) in self.package_scripts
        )

    def unresolved_check(self, span: str) -> str | None:
        """Why a span shaped like a check citation names nothing, or None."""
        if TEST_NAME.match(span) and span not in self.test_names:
            return f"`{span}` is shaped like a test name, but no tracked test defines it"
        if CHECK_NAME.match(span) and span not in self.check_names:
            return (
                f"`{span}` is shaped like a check script name, but no tracked check script has it"
            )
        script = _pnpm_script(span)
        if script and script not in self.package_scripts:
            return f"`{span}` runs `{script}`, but no tracked package.json defines that script"
        group = _group(span)
        if group and group not in self.check_groups:
            return f"`{span}` runs check group `{group}`, but `.repoctl/checks.toml` defines no such group"
        biome = BIOME_RULE.match(span)
        if biome and span.startswith("lint/") and not self.enables_lint(span):
            return f"`{span}` is shaped like a Biome rule, but the configuration does not enable it"
        return None


def _group(span: str) -> str:
    """The check group a `repoctl run-checks --group` span runs, or empty."""
    match = GROUP_SPAN.match(span)
    return "" if match is None else match.group("group")


def _pnpm_script(span: str) -> str:
    """The namespaced package script a `pnpm ...` span runs, or empty."""
    match = PNPM_SPAN.match(span)
    if match is None:
        return ""
    return next((arg for arg in match.group("args").split() if PNPM_SCRIPT.match(arg)), "")
