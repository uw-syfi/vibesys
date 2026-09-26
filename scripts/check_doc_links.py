#!/usr/bin/env python3
"""Fail when a Markdown link points at something that does not exist.

Two link classes break in ways nothing else in CI catches:

1.  A relative link whose target was renamed or deleted. Nothing type-checks
    Markdown, so the link rots silently until a reader clicks it.
2.  A link inside `docs/` that escapes `docs/`. Those resolve on GitHub but
    not on the docs website: the Docusaurus docs plugin serves `docs/` as its
    whole content root, so `../src/...` resolves to a site URL that was never
    built. Such links must use an absolute repository URL instead.

Checked: relative links and absolute links back into this repository
(`https://github.com/uw-syfi/vibesys/blob|tree/<ref>/<path>`), including their
`#anchor` when the target is Markdown. Other external URLs are not fetched,
so the check stays offline and deterministic.

Usage:
    uv run python scripts/check_doc_links.py [PATH ...].
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_URL_PREFIXES = (
    "https://github.com/uw-syfi/vibesys/blob/",
    "https://github.com/uw-syfi/vibesys/tree/",
)

# Subtrees the docs website publishes as its own content root. A relative link
# from inside one of these must stay inside it.
PUBLISHED_ROOTS = ("docs",)

# Vendored trees. Their links belong to the upstream project and are broken
# there too (they point at pages of the original site that were not mirrored),
# so enforcing them here would only pin us to upstream's bugs.
EXCLUDED_PREFIXES = (
    "third_party/",
    "node_modules/",
    "resources/skills/neuron-agentic-development/",
)
MARKDOWN_SUFFIXES = (".md", ".mdx")

# Inline `[text](target)` links plus `[id]: target` reference definitions.
INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s<>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+)>?", re.MULTILINE)
FENCE = re.compile(r"^\s*(```|~~~)")

EXPLICIT_HEADING_ID = re.compile(r"\{#([^}]+)\}\s*$")
HTML_ANCHOR = re.compile(r"""(?:id|name)=["']([^"']+)["']""")


@dataclass(frozen=True)
class Link:
    """One Markdown link, located at ``source``:``line``."""

    source: Path
    line: int
    target: str


@dataclass(frozen=True)
class Problem:
    """A link that failed a check, with the reason it failed."""

    link: Link
    reason: str


def iter_markdown_files(paths: list[Path], repo_root: Path) -> list[Path]:
    """List tracked Markdown files under ``paths`` (whole repo when empty)."""
    argv = ["git", "-C", str(repo_root), "ls-files", "-z"]
    argv += [str(p) for p in paths] if paths else ["*.md", "*.mdx"]
    out = run_git(argv)
    files = []
    for name in filter(None, out.split("\0")):
        if not name.endswith(MARKDOWN_SUFFIXES):
            continue
        if name.startswith(EXCLUDED_PREFIXES):
            continue
        files.append(repo_root / name)
    return sorted(files)


def run_git(argv: list[str]) -> str:
    """Run a git command and return its stdout.

    Argv is built from repo-relative paths supplied on the command line, never
    from file contents, so `git` off PATH is the same trust boundary as the
    rest of `scripts/`.
    """
    # lint-waiver: LW-008043 [S603]; Callers build Git argv from fixed subcommands and repo-relative paths.
    result = subprocess.run(argv, capture_output=True, text=True, check=True)  # noqa: S603
    return result.stdout


def extract_links(path: Path) -> list[Link]:
    """Collect every inline and reference link in ``path``, skipping code fences."""
    links: list[Link] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        text = re.sub(r"`[^`]*`", "", line)
        links.extend(Link(path, lineno, m.group(1)) for m in INLINE_LINK.finditer(text))
        links.extend(Link(path, lineno, m.group(1)) for m in REFERENCE_LINK.finditer(text))
    return links


def slugify(heading: str) -> str:
    """Approximate the GitHub heading-anchor slug for ``heading``."""
    text = heading.strip().lstrip("#").strip()
    text = EXPLICIT_HEADING_ID.sub("", text).strip()
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text.strip()).lower()


def anchors_of(path: Path) -> set[str]:
    """Every anchor a Markdown file exposes: heading slugs, `{#id}`, HTML ids.

    Repeated headings get GitHub's `-1`, `-2`, ... disambiguating suffixes.
    """
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in path.read_text().splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        anchors.update(HTML_ANCHOR.findall(line))
        if not line.lstrip().startswith("#"):
            continue
        explicit = EXPLICIT_HEADING_ID.search(line)
        if explicit:
            anchors.add(explicit.group(1))
        slug = slugify(line)
        if not slug:
            continue
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def published_root_of(rel_path: Path) -> str | None:
    """Return the published subtree containing ``rel_path``, if any."""
    top = rel_path.parts[0] if rel_path.parts else ""
    return top if top in PUBLISHED_ROOTS else None


def resolve(link: Link, repo_root: Path) -> tuple[Path, str] | None:
    """Map ``link`` to a repo path and anchor, or None when it is not checkable."""
    target = link.target
    for prefix in REPO_URL_PREFIXES:
        if target.startswith(prefix):
            # Strip the git ref segment that follows blob/ or tree/.
            _ref, _, path_part = target[len(prefix) :].partition("/")
            split = urlsplit(path_part)
            if not split.path:
                return None
            return repo_root / unquote(split.path), split.fragment
    if "://" in target or target.startswith(("mailto:", "#", "/")):
        return None
    split = urlsplit(target)
    if not split.path:
        return None
    return (link.source.parent / unquote(split.path)).resolve(), split.fragment


def check(files: list[Path], repo_root: Path) -> list[Problem]:
    """Report every link in ``files`` whose target or anchor does not exist."""
    problems: list[Problem] = []
    for path in files:
        source_rel = path.relative_to(repo_root)
        for link in extract_links(path):
            resolved = resolve(link, repo_root)
            if resolved is None:
                continue
            target, anchor = resolved
            if not target.exists():
                problems.append(Problem(link, "target does not exist"))
                continue
            root = published_root_of(source_rel)
            is_relative_link = not link.target.startswith("https://")
            if root is not None and is_relative_link:
                try:
                    target.relative_to(repo_root / root)
                except ValueError:
                    problems.append(
                        Problem(
                            link,
                            f"relative link escapes the published `{root}/` tree "
                            "(breaks on the docs website); use an absolute "
                            "https://github.com/uw-syfi/vibesys/... URL",
                        )
                    )
                    continue
            if (
                anchor
                and target.suffix in MARKDOWN_SUFFIXES
                and anchor.lower() not in anchors_of(target)
            ):
                problems.append(Problem(link, f"no `#{anchor}` anchor in target"))
    return problems


def main(argv: list[str] | None = None) -> int:
    """Check every selected Markdown file and print each broken link."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Limit the check to these files or directories (default: whole repo).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(run_git(["git", "rev-parse", "--show-toplevel"]).strip())
    files = iter_markdown_files(args.paths, repo_root)
    problems = check(files, repo_root)
    for problem in problems:
        rel = problem.link.source.relative_to(repo_root)
        print(f"{rel}:{problem.link.line}: {problem.link.target}: {problem.reason}")
    if problems:
        print(f"\n{len(problems)} broken Markdown link(s) in {len(files)} file(s).")
        return 1
    print(f"Checked {len(files)} Markdown file(s); no broken links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
