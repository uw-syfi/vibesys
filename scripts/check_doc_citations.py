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

Usage:
    python3 -m scripts.check_doc_citations [PATH ...].

Run as a module, not by path: the lexer comes from the sibling script, so the
repository root has to be on `sys.path`. Stdlib only, like that sibling, so it
needs no dependency setup in a docs-only CI job.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
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
