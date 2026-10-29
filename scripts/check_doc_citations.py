"""Fail when a code citation in the docs does not resolve to the code it names.

A sentence that describes code and points at it by line range decays silently:
the cited file grows, the range still exists, and the citation now names
unrelated code. All eleven ranges in `docs/contributing/wire-protocol.md` had
drifted that way before this check existed (#1056), two of them onto an error
class instead of the request loop they claimed to show.
`scripts/check_doc_links.py` cannot see any of it, because it strips the inline
code spans a citation lives in.

What counts as a citation:
    An inline code span whose *entire* content is `<path><sep><locator>`.

    path
        A file path with an alphabetic extension, matched against the tracked
        files as a segment-aligned suffix, so `unix_jsonl.py` and
        `src/server/transport/unix_jsonl.py` both resolve, and an ambiguous
        suffix fails until it is qualified.
    sep
        `:`, or pytest's `::`.
    locator
        A symbol name, optionally qualified by its owner
        (`_RequestHandler.handle`), or a line number or line range.

    A span holding only a line range (`:184-190`, continuing the file an
    earlier citation named) is a citation too, and always fails: it is the one
    shape that evades the grammar above while still reading as a citation.

    Everything else in a doc is prose and is ignored: a path with no locator
    (`protocol.py`), a span that is not a path (`extra="forbid"`), a host and
    port (`127.0.0.1:8765`, whose last dot-segment is not alphabetic), a URL,
    and anything inside a fenced code block. A bare path remains prose even in
    a gated doc: documentation must be able to name local files and external
    reference checkouts without claiming a checked code citation. Add a
    locator when the sentence needs that stronger claim. The lexer deciding
    what an inline code span is belongs to `scripts/check_doc_links.py`, which
    strips exactly the spans this script reads, so the two cannot disagree
    about where a citation can hide.

The rule:
    A line locator always fails. Nothing about `foo.py:120-138` is checkable:
    every range inside a long enough file exists, so a check that the line
    exists would pass on all eleven drifted citations above. Cite the symbol
    instead. A symbol locator must be defined in the cited file: an AST
    definition for Python (`def`, `class`, or an assignment target, bare or
    qualified), a word-boundary occurrence of the name for any other text
    file.

    This errs toward false positives. A line range that happens to be right
    today is still rejected, and a path suffix matching two tracked files fails
    until it is disambiguated; both are cheap to fix while writing the
    sentence. The residual false negative is worth stating so it is not
    mistaken for a guarantee: a symbol that still exists but no longer does
    what the sentence says passes. A symbol reference survives every edit that
    does not rename or delete it, which is the common case, so the rename and
    the deletion are what is left to catch, and they are what this catches.

Scope:
    With no arguments this checks `GATED_DOCS`, the docs whose citations have
    been swept and are kept honest; a `GATED_DOCS` entry that no longer exists
    is itself a failure, so the gate cannot go quiet. Grow the tuple as other
    docs are swept. Pass paths to survey anything else, for instance
    `python3 -m scripts.check_doc_citations .` for the whole repository. Those
    failures are reported but not gated.

Strict references:
    `check_references` adds two reference kinds to the citations above, for
    trees that must not name anything that no longer exists (the agent skills,
    through `scripts/check_skills.py`). A *dotted name* is an inline code span
    that is entirely `<package>.<name>[.<name>...]` and whose first segment is
    a top-level package under one of the `source_roots` in `tach.toml`
    (`vibesys.api`, `vs_runtime.api.AgentSessions`); it resolves when the
    longest module prefix exists and every remaining segment is defined in,
    imported into, or star-imported into that module. A *repo path* is an
    inline code span that is entirely a slash-separated path whose first
    segment is a tracked top-level directory (`scripts/check_purity.py`,
    `tests/quality/`); it resolves when it names a tracked file or a directory
    holding one, and a `*` or `?` glob resolves when it matches one. A span
    holding a placeholder (`<pkg>`, `{name}`) or a space is prose. The gated
    docs keep the narrower citation rule: a bare path there stays prose.

Usage:
    python3 -m scripts.check_doc_citations [PATH ...].

Run as a module, not by path: the lexer comes from the sibling script, so the
repository root has to be on `sys.path`. Stdlib only, like that sibling, so it
needs no dependency setup in a docs-only CI job.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from scripts.check_doc_links import iter_code_spans, iter_markdown_files, run_git

# Docs whose citations are swept and gated. One entry per doc, so opting a doc
# in is a reviewed decision rather than a repo-wide sweep nobody asked for.
GATED_DOCS = ("docs/contributing/wire-protocol.md",)

CITATION = re.compile(r"^(?P<path>[\w./-]+\.[A-Za-z][\w+]*)::?(?P<locator>[^\s`]+)$")
# A line range on its own, continuing the file named by an earlier citation.
CONTINUED_CITATION = re.compile(r"^:(?P<locator>\d+(?:-\d+)?)$")
LINE_LOCATOR = re.compile(r"^\d+(?:-\d+)?$")
SYMBOL_LOCATOR = re.compile(r"^[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*$")
SYMBOL_SEPARATOR = re.compile(r"\.|::")
DOTTED_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
REPO_PATH = re.compile(r"^[\w.-]+(?:/[\w.*?-]+)*/?$")
# A dotted span ending in one of these is a file name (`vibesys.input.toml`), not a module.
FILE_SUFFIXES = frozenset(
    {"j2", "json", "jsonl", "md", "py", "sh", "toml", "ts", "txt", "yaml", "yml"}
)
# Directories a build or an installer fills; nothing under them is tracked.
BUILD_OUTPUT_SEGMENTS = frozenset({"dist", "build", "node_modules", ".venv", "__pycache__"})
# How many re-exports a dotted name may pass through before the lookup gives up.
MAX_REEXPORT_HOPS = 8

LINE_LOCATOR_REASON = (
    "cites a line number, which nothing can check: every range inside a long "
    "enough file exists. Cite the symbol that the sentence describes instead"
)
CONTINUED_CITATION_REASON = (
    "is a line range with no file, continuing an earlier citation. Write every "
    "citation in full, and cite a symbol rather than a line"
)

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2


@dataclass(frozen=True)
class Citation:
    """One `path:locator` code citation, written at ``source``:``line``."""

    source: Path
    line: int
    text: str
    path: str
    locator: str


@dataclass(frozen=True)
class Problem:
    """A citation that failed a check, with the reason it failed."""

    citation: Citation
    reason: str


def extract_citations(source: Path, text: str) -> list[Citation]:
    """Collect every code citation in ``text``, skipping fenced code blocks."""
    citations: list[Citation] = []
    for span in iter_code_spans(text):
        match = CITATION.match(span.text) or CONTINUED_CITATION.match(span.text)
        if match is None:
            continue
        groups = match.groupdict()
        citations.append(
            Citation(
                source=source,
                line=span.line,
                text=span.text,
                path=groups.get("path", ""),
                locator=groups["locator"],
            )
        )
    return citations


def tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Every repo-relative path `git ls-files` reports for ``repo_root``."""
    listing = run_git(["git", "-C", str(repo_root), "ls-files", "-z"])
    return tuple(filter(None, listing.split("\0")))


def matching_paths(cited: str, tracked: tuple[str, ...]) -> list[str]:
    """Tracked paths ``cited`` names, exactly or as a segment-aligned suffix."""
    if cited in tracked:
        return [cited]
    suffix = f"/{cited}"
    return sorted(path for path in tracked if path.endswith(suffix))


def python_definitions(text: str) -> set[str]:
    """Every name a Python module defines, bare and qualified by its owner.

    Raises:
        SyntaxError: If ``text`` is not parseable Python.
    """
    definitions: set[str] = set()
    _collect_definitions(ast.parse(text), "", definitions)
    return definitions


def _collect_definitions(node: ast.AST, prefix: str, into: set[str]) -> None:
    """Add every name defined under ``node`` to ``into``, under ``prefix``."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            into.update({child.name, f"{prefix}{child.name}"})
            _collect_definitions(child, f"{prefix}{child.name}.", into)
            continue
        for name in _assigned_names(child):
            into.update({name, f"{prefix}{name}"})


def _assigned_names(node: ast.AST) -> list[str]:
    """Plain names bound by ``node`` when it is an assignment statement."""
    if isinstance(node, ast.Assign):
        targets: list[ast.expr] = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    elif type(node).__name__ == "TypeAlias":
        # `type X = ...`, matched by name: CI's docs job may run an interpreter whose `ast`
        # predates `TypeAlias` (and cannot parse one). Its first child node is the bound name.
        targets = [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.Name)][:1]
    else:
        return []
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple):
            names.extend(item.id for item in target.elts if isinstance(item, ast.Name))
    return names


def unresolved_symbol_reason(target: Path, relative: str, symbol: str) -> str | None:
    """Why ``symbol`` does not resolve in ``target``, or None when it does."""
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"`{relative}` cannot be read as text ({exc})"
    if target.suffix == ".py":
        try:
            definitions = python_definitions(text)
        except SyntaxError as exc:
            return f"`{relative}` does not parse as Python ({exc})"
        if symbol.replace("::", ".") in definitions:
            return None
        return f"`{relative}` defines no `{symbol}`"
    name = SYMBOL_SEPARATOR.split(symbol)[-1]
    if re.search(rf"\b{re.escape(name)}\b", text):
        return None
    return f"`{relative}` does not mention `{name}`"


def unresolved_reason(citation: Citation, tracked: tuple[str, ...], repo_root: Path) -> str | None:
    """Why ``citation`` does not resolve, or None when it does."""
    if not citation.path:
        return CONTINUED_CITATION_REASON
    matches = matching_paths(citation.path, tracked)
    if not matches:
        return "no tracked file has this path"
    if len(matches) > 1:
        listed = ", ".join(f"`{path}`" for path in matches)
        return f"path is ambiguous: it matches {listed}. Cite the full path"
    if LINE_LOCATOR.match(citation.locator):
        return LINE_LOCATOR_REASON
    if not SYMBOL_LOCATOR.match(citation.locator):
        return "locator is neither a symbol name nor a line range"
    (relative,) = matches
    return unresolved_symbol_reason(repo_root / relative, relative, citation.locator)


def check(files: list[Path], repo_root: Path, tracked: tuple[str, ...]) -> list[Problem]:
    """Report every citation in ``files`` that does not resolve in ``tracked``."""
    problems: list[Problem] = []
    for path in files:
        for citation in extract_citations(path, path.read_text(encoding="utf-8")):
            reason = unresolved_reason(citation, tracked, repo_root)
            if reason is not None:
                problems.append(Problem(citation, reason))
    return problems


@dataclass(frozen=True)
class RepoIndex:
    """What strict reference checks resolve against: the tracked tree and its packages.

    ``packages`` maps each top-level Python package to the source roots that
    hold it, read from `tach.toml`, so a new library is covered once tach knows it.
    """

    root: Path
    tracked: tuple[str, ...]
    top_dirs: frozenset[str]
    packages: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Finding:
    """One reference in ``source`` at ``line`` that does not resolve, and why."""

    source: Path
    line: int
    text: str
    reason: str


def repo_index(root: Path, tracked: tuple[str, ...]) -> RepoIndex:
    """Index ``tracked`` under ``root``: top-level directories and tach's packages."""
    top_dirs = frozenset(path.split("/", 1)[0] for path in tracked if "/" in path)
    tach = root / "tach.toml"
    source_roots: list[str] = []
    if tach.is_file():
        config = tomllib.loads(tach.read_text(encoding="utf-8"))
        source_roots = [str(entry).rstrip("/") for entry in config.get("source_roots", [])]
    packages: dict[str, list[str]] = {}
    for source_root in source_roots:
        for path in tracked:
            parts = path.removeprefix(f"{source_root}/").split("/")
            if path.startswith(f"{source_root}/") and parts[1:] == ["__init__.py"]:
                packages.setdefault(parts[0], []).append(source_root)
    return RepoIndex(
        root, tracked, top_dirs, {name: tuple(roots) for name, roots in packages.items()}
    )


def is_dotted_name(text: str, index: RepoIndex) -> bool:
    """Whether a code span is a dotted name into one of the repo's packages."""
    if not DOTTED_NAME.match(text):
        return False
    parts = text.split(".")
    return parts[0] in index.packages and parts[-1] not in FILE_SUFFIXES


def is_repo_path(text: str, index: RepoIndex) -> bool:
    """Whether a code span is a path into a tracked top-level directory.

    A path through a build-output directory (`clients/apps/desktop-shell/dist/main.js`)
    names a file a build makes, which is never tracked, so it stays prose.
    """
    if "/" not in text or not REPO_PATH.match(text):
        return False
    segments = text.rstrip("/").split("/")
    return segments[0] in index.top_dirs and not BUILD_OUTPUT_SEGMENTS.intersection(segments)


def is_local_path(text: str, index: RepoIndex, base: str) -> bool:
    """Whether a code span is a path into a tracked directory directly under ``base``."""
    if "/" not in text or not REPO_PATH.match(text):
        return False
    first = f"{base.rstrip('/')}/{text.split('/', 1)[0]}/"
    return any(tracked.startswith(first) for tracked in index.tracked)


def _module_file(parts: list[str], index: RepoIndex) -> tuple[str, str] | None:
    """The source root and tracked file of the module ``parts`` names, if any."""
    for source_root in index.packages.get(parts[0], ()):
        base = "/".join([source_root, *parts])
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in index.tracked:
                return source_root, candidate
    return None


def _imports(
    module: list[str], tree: ast.Module, *, is_package: bool
) -> tuple[dict[str, str], list[str]]:
    """Names ``tree`` imports, mapped to their absolute dotted origin, and its star sources.

    ``module`` is the importing module's dotted name as parts; ``is_package``
    says whether it is a package's `__init__.py`, which relative imports start from.
    """
    package = module if is_package else module[:-1]
    named: dict[str, str] = {}
    stars: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = node.module or ""
        if node.level:
            base = package[: len(package) - (node.level - 1)]
            source = ".".join(filter(None, [*base, source]))
        for alias in node.names:
            if alias.name == "*":
                stars.append(source)
            else:
                named[alias.asname or alias.name] = f"{source}.{alias.name}"
    return named, stars


def unresolved_dotted_name_reason(name: str, index: RepoIndex, hops: int = 0) -> str | None:
    """Why the dotted ``name`` does not resolve to a module or a name in one."""
    parts = name.split(".")
    for split in range(len(parts), 0, -1):
        found = _module_file(parts[:split], index)
        if found is not None:
            return _unresolved_attribute_reason(found[1], parts[:split], parts[split:], index, hops)
    return f"no module `{parts[0]}` under the `tach.toml` source roots"


def _unresolved_attribute_reason(
    module_file: str, module: list[str], attrs: list[str], index: RepoIndex, hops: int
) -> str | None:
    """Why ``attrs`` is not defined in, or imported into, ``module_file``."""
    if not attrs:
        return None
    try:
        tree = ast.parse((index.root / module_file).read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return f"`{module_file}` does not parse as Python ({exc})"
    definitions: set[str] = set()
    _collect_definitions(tree, "", definitions)
    named, stars = _imports(module, tree, is_package=module_file.endswith("/__init__.py"))
    origins = [named[attrs[0]]] if attrs[0] in named else []
    origins += [f"{star}.{attrs[0]}" for star in stars]
    if ".".join(attrs) in definitions or _any_origin_resolves(origins, attrs[1:], index, hops):
        return None
    return f"`{module_file}` defines or imports no `{'.'.join(attrs)}`"


def _any_origin_resolves(origins: list[str], rest: list[str], index: RepoIndex, hops: int) -> bool:
    """Whether a name imported from one of ``origins`` resolves, followed by ``rest``.

    An origin outside the repository's packages counts as resolving: there is
    nothing in this repository to check it against.
    """
    if hops >= MAX_REEXPORT_HOPS:
        return False
    return any(
        origin.split(".", 1)[0] not in index.packages
        or unresolved_dotted_name_reason(".".join([origin, *rest]), index, hops + 1) is None
        for origin in origins
    )


def unresolved_repo_path_reason(path: str, index: RepoIndex, base: str = "") -> str | None:
    """Why ``path`` names no tracked file or directory, or None when it does.

    ``base`` is a repo-relative directory the path may also be relative to,
    such as the skill directory whose own `scripts/` a skill names.
    """
    stems = [path.rstrip("/")]
    if base:
        stems.append(f"{base.rstrip('/')}/{stems[0]}")
    for stem in stems:
        if any(char in stem for char in "*?"):
            matched = any(
                fnmatch.fnmatch(tracked, stem) or fnmatch.fnmatch(tracked, f"{stem}/*")
                for tracked in index.tracked
            )
        else:
            matched = stem in index.tracked or any(
                tracked.startswith(f"{stem}/") for tracked in index.tracked
            )
        if matched:
            return None
    return "no tracked file or directory has this path"


def check_references(files: list[Path], index: RepoIndex, base: str = "") -> list[Finding]:
    """Every citation, dotted name and repo path in ``files`` that does not resolve.

    A repo path may also be relative to ``base`` (see `unresolved_repo_path_reason`).
    """
    findings: list[Finding] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for citation in extract_citations(path, text):
            reason = unresolved_reason(citation, index.tracked, index.root)
            if reason is not None:
                findings.append(Finding(path, citation.line, citation.text, reason))
        for span in iter_code_spans(text):
            if is_dotted_name(span.text, index):
                reason = unresolved_dotted_name_reason(span.text, index)
            elif is_repo_path(span.text, index) or (base and is_local_path(span.text, index, base)):
                reason = unresolved_repo_path_reason(span.text, index, base)
            else:
                continue
            if reason is not None:
                findings.append(Finding(path, span.line, span.text, reason))
    return findings


def stale_gate_entries(tracked: tuple[str, ...]) -> list[str]:
    """`GATED_DOCS` entries that no longer name a tracked file."""
    return [doc for doc in GATED_DOCS if doc not in tracked]


def main(argv: list[str] | None = None) -> int:
    """Check the selected docs and print every citation that does not resolve."""
    parser = argparse.ArgumentParser(description="Check `file:symbol` code citations in the docs.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Survey these files or directories instead of the gated docs.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(run_git(["git", "rev-parse", "--show-toplevel"]).strip())
    tracked = tracked_paths(repo_root)
    if not args.paths:
        stale = stale_gate_entries(tracked)
        if stale:
            print(
                "check_doc_citations: GATED_DOCS entries no longer exist; update the tuple in "
                f"scripts/check_doc_citations.py: {', '.join(stale)}",
                file=sys.stderr,
            )
            return EXIT_TOOL_ERROR
    selected = args.paths or [Path(doc) for doc in GATED_DOCS]
    files = iter_markdown_files(selected, repo_root)
    problems = check(files, repo_root, tracked)
    for problem in problems:
        citation = problem.citation
        rel = citation.source.relative_to(repo_root)
        print(f"{rel}:{citation.line}: `{citation.text}`: {problem.reason}")
    if problems:
        print(f"\n{len(problems)} unresolved code citation(s) in {len(files)} file(s).")
        return EXIT_VIOLATIONS
    print(f"Checked {len(files)} Markdown file(s); every code citation resolves.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
