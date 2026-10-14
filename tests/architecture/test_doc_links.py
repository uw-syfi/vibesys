"""Contracts for the Markdown link checker used by CI.

The checker's whole value is catching links that the Docusaurus build cannot
see, so these tests pin the cases it must report. A link checker that silently
matches nothing still exits 0, which is indistinguishable from a clean repo,
so every "is reported" case here is paired with the accepted counterpart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from scripts.check_doc_links import (
    anchors_of,
    check,
    check_orphans,
    extract_links,
    find_orphans,
    sidebar_doc_ids,
)

if TYPE_CHECKING:
    from pathlib import Path


def _repo(root: Path, files: dict[str, str]) -> Path:
    """Materialize a fake repository from a path -> contents mapping."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _reasons(root: Path, *names: str) -> list[str]:
    problems = check([root / name for name in names], root)
    return [problem.reason for problem in problems]


def test_relative_link_to_missing_file_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/a.md": "# A\n\n[gone](./b.md)\n"})

    assert _reasons(root, "docs/a.md") == ["target does not exist"]


def test_relative_link_between_siblings_in_docs_is_accepted(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/a.md": "# A\n\n[b](b.md)\n", "docs/b.md": "# B\n"})

    assert _reasons(root, "docs/a.md") == []


def test_relative_link_escaping_the_published_tree_is_reported(tmp_path: Path) -> None:
    """The defect that shipped: valid on GitHub, 404 on the docs site."""
    root = _repo(
        tmp_path,
        {"docs/a.md": "# A\n\n[flags](../src/FLAGS.md)\n", "src/FLAGS.md": "# Flags\n"},
    )

    (reason,) = _reasons(root, "docs/a.md")
    assert "escapes the published `docs/` tree" in reason


def test_relative_link_outside_docs_may_escape_its_own_directory(tmp_path: Path) -> None:
    """Only `docs/` is published, so the rule must not apply repo-wide."""
    root = _repo(
        tmp_path,
        {"src/a.md": "# A\n\n[flags](../other/FLAGS.md)\n", "other/FLAGS.md": "# Flags\n"},
    )

    assert _reasons(root, "src/a.md") == []


def test_absolute_repo_url_to_missing_path_is_reported(tmp_path: Path) -> None:
    body = "# A\n\n[x](https://github.com/uw-syfi/vibesys/blob/main/src/gone.py)\n"
    root = _repo(tmp_path, {"docs/a.md": body})

    assert _reasons(root, "docs/a.md") == ["target does not exist"]


def test_absolute_repo_url_to_existing_path_is_accepted(tmp_path: Path) -> None:
    body = "# A\n\n[x](https://github.com/uw-syfi/vibesys/blob/main/src/FLAGS.md)\n"
    root = _repo(tmp_path, {"docs/a.md": body, "src/FLAGS.md": "# Flags\n"})

    assert _reasons(root, "docs/a.md") == []


def test_third_party_urls_are_not_treated_as_repo_paths(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/a.md": "# A\n\n[uv](https://docs.astral.sh/uv/)\n"})

    assert _reasons(root, "docs/a.md") == []


def test_missing_anchor_is_reported(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"docs/a.md": "# A\n\n[b](b.md#nope)\n", "docs/b.md": "# B\n\n## Real Heading\n"},
    )

    assert _reasons(root, "docs/a.md") == ["no `#nope` anchor in target"]


def test_existing_anchor_is_accepted(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "docs/a.md": "# A\n\n[b](b.md#real-heading)\n",
            "docs/b.md": "# B\n\n## Real Heading\n",
        },
    )

    assert _reasons(root, "docs/a.md") == []


def test_links_inside_code_fences_are_ignored(tmp_path: Path) -> None:
    body = "# A\n\n```\n[not a link](./gone.md)\n```\n"
    root = _repo(tmp_path, {"docs/a.md": body})

    assert extract_links(root / "docs/a.md") == []
    assert _reasons(root, "docs/a.md") == []


def test_reference_style_definitions_are_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/a.md": "# A\n\nSee [ref].\n\n[ref]: ./gone.md\n"})

    assert _reasons(root, "docs/a.md") == ["target does not exist"]


def test_anchors_cover_explicit_ids_and_repeated_headings(tmp_path: Path) -> None:
    body = "# T\n\n## Setup {#install}\n\n## Notes\n\n## Notes\n"
    root = _repo(tmp_path, {"docs/a.md": body})

    anchors = anchors_of(root / "docs/a.md")

    assert {"install", "notes", "notes-1"} <= anchors


def _orphans(root: Path, files: dict[str, str], sidebar: str = "") -> list[str]:
    _repo(root, files)
    return find_orphans([root / name for name in sorted(files)], root, sidebar_doc_ids(sidebar))


def test_a_docs_page_nothing_links_to_is_an_orphan(tmp_path: Path) -> None:
    assert _orphans(tmp_path, {"docs/a.md": "# A\n", "README.md": "# R\n"}) == ["docs/a.md"]


def test_a_docs_page_linked_from_outside_docs_is_reached(tmp_path: Path) -> None:
    files = {"docs/a.md": "# A\n", "README.md": "[a](docs/a.md)\n"}

    assert _orphans(tmp_path, files) == []


def test_a_docs_page_named_in_the_sidebar_is_reached(tmp_path: Path) -> None:
    files = {"docs/sub/a.md": "# A\n"}

    assert _orphans(tmp_path, files, "items: ['sub/a']") == []


def test_a_self_link_or_a_backticked_path_does_not_reach_a_page(tmp_path: Path) -> None:
    files = {"docs/a.md": "[me](a.md)\n", "README.md": "See `docs/a.md`.\n"}

    assert _orphans(tmp_path, files) == ["docs/a.md"]


def test_pages_outside_docs_are_never_orphans(tmp_path: Path) -> None:
    assert _orphans(tmp_path, {"src/README.md": "# S\n"}) == []


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@given(
    edges=st.sets(st.tuples(st.integers(0, 5), st.integers(0, 5))),
    in_sidebar=st.sets(st.integers(0, 5)),
)
def test_orphans_are_exactly_the_pages_without_an_inbound_edge(
    tmp_path: Path, edges: set[tuple[int, int]], in_sidebar: set[int]
) -> None:
    # Every example writes the same six pages, so reusing `tmp_path` is safe.
    files = {
        f"docs/p{page}.md": "".join(f"[x](p{dst}.md)\n" for src, dst in edges if src == page)
        for page in range(6)
    }
    sidebar = " ".join(f"'p{page}'" for page in in_sidebar)
    reached = {dst for src, dst in edges if src != dst} | in_sidebar

    assert _orphans(tmp_path, files, sidebar) == sorted(
        f"docs/p{page}.md" for page in range(6) if page not in reached
    )


def test_an_unrecorded_orphan_fails_with_its_path() -> None:
    (message,) = check_orphans(["docs/new.md"], frozenset())

    assert message.startswith("docs/new.md: ")


def test_a_recorded_orphan_that_gained_a_link_fails_until_removed() -> None:
    assert check_orphans(["docs/old.md"], frozenset({"docs/old.md"})) == []
    (message,) = check_orphans([], frozenset({"docs/old.md"}))
    assert message.startswith("docs/old.md: ")
    assert "remove the entry" in message
