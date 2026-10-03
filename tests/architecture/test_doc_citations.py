"""Contracts for the code-citation checker used by CI.

The checker exists because a `file:line` citation decays invisibly: the cited
file grows, the range still resolves, and the sentence now points at unrelated
code (#1056). So the cases pinned here are the ones that distinguish it from a
checker that only asks whether a line exists: a line locator always fails, a
symbol locator is verified against the cited file, and prose that merely names
a file is left alone. A checker that matched nothing would also exit 0, so
every "is reported" case is paired with its accepted counterpart.
"""

from __future__ import annotations

import keyword
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_doc_citations import (
    GATED_DOCS,
    Citation,
    check,
    extract_citations,
    python_definitions,
    stale_gate_entries,
    tracked_paths,
    unresolved_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

IDENTIFIERS = st.from_regex(r"\A[a-z][a-z0-9_]{0,10}\Z").filter(
    lambda name: not keyword.iskeyword(name)
)
LINE_NUMBERS = st.integers(min_value=1, max_value=10**6)

HANDLER_MODULE = """
CONSTANT = 1


class Handler:
    def handle(self) -> None:
        pass


def free_function() -> None:
    pass
"""


def _repo(root: Path, files: dict[str, str]) -> tuple[str, ...]:
    """Materialize a fake repository and return its tracked paths."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tuple(files)


def _reasons(root: Path, doc: str, files: dict[str, str]) -> list[str]:
    """Materialize ``files`` as a repository and check ``doc``'s citations."""
    tracked = _repo(root, files)
    return [problem.reason for problem in check([root / doc], root, tracked)]


def _citations(source: str) -> list[str]:
    """The citation spans `extract_citations` finds in ``source``."""
    return [citation.text for citation in extract_citations(Path("doc.md"), source)]


def test_citation_to_a_defined_symbol_is_accepted(tmp_path: Path) -> None:
    files = {"docs/a.md": "See `mod.py:free_function`.\n", "mod.py": HANDLER_MODULE}

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_citation_to_a_method_qualified_by_its_class_is_accepted(tmp_path: Path) -> None:
    files = {"docs/a.md": "See `mod.py:Handler.handle`.\n", "mod.py": HANDLER_MODULE}

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_citation_to_a_module_constant_is_accepted(tmp_path: Path) -> None:
    files = {"docs/a.md": "See `mod.py:CONSTANT`.\n", "mod.py": HANDLER_MODULE}

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_pytest_style_double_colon_separator_is_accepted(tmp_path: Path) -> None:
    """The repo already cites some code as `file.py::Symbol`; both separators work."""
    files = {"docs/a.md": "See `mod.py::Handler`.\n", "mod.py": HANDLER_MODULE}

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_citation_to_a_renamed_symbol_is_reported_with_its_doc_location(tmp_path: Path) -> None:
    """The failure the checker exists for: the sentence outlived the symbol."""
    files = {
        "docs/a.md": "# A\n\nThe write path is `mod.py:_write_messag`.\n",
        "mod.py": HANDLER_MODULE,
    }
    tracked = _repo(tmp_path, files)

    (citation,) = extract_citations(tmp_path / "docs/a.md", files["docs/a.md"])
    reason = unresolved_reason(citation, tracked, tmp_path)

    assert citation.source == tmp_path / "docs/a.md"
    assert citation.line == 3
    assert reason == "`mod.py` defines no `_write_messag`"


def test_line_range_citation_is_reported_even_when_the_lines_exist(tmp_path: Path) -> None:
    """Every drifted citation in `wire-protocol.md` named lines that existed."""
    files = {"docs/a.md": "See `mod.py:5-7`.\n", "mod.py": HANDLER_MODULE}

    (reason,) = _reasons(tmp_path, "docs/a.md", files)

    assert "cites a line number" in reason


def test_bare_line_range_continuing_an_earlier_citation_is_reported(tmp_path: Path) -> None:
    """`(`mod.py:1-2`, `:5-7`)` must not smuggle the second range past the grammar."""
    files = {"docs/a.md": "See `:5-7`.\n", "mod.py": HANDLER_MODULE}

    (reason,) = _reasons(tmp_path, "docs/a.md", files)

    assert "line range with no file" in reason


def test_citation_to_a_path_no_tracked_file_has_is_reported(tmp_path: Path) -> None:
    files = {"docs/a.md": "See `gone.py:free_function`.\n", "mod.py": HANDLER_MODULE}

    assert _reasons(tmp_path, "docs/a.md", files) == ["no tracked file has this path"]


def test_path_suffix_matching_two_tracked_files_is_reported(tmp_path: Path) -> None:
    """A bare filename is convenient only while it names one file."""
    files = {
        "docs/a.md": "See `mod.py:free_function`.\n",
        "one/mod.py": HANDLER_MODULE,
        "two/mod.py": HANDLER_MODULE,
    }

    (reason,) = _reasons(tmp_path, "docs/a.md", files)

    assert "ambiguous" in reason
    assert "`one/mod.py`, `two/mod.py`" in reason


def test_full_path_is_accepted_when_a_bare_filename_would_be_ambiguous(tmp_path: Path) -> None:
    files = {
        "docs/a.md": "See `one/mod.py:free_function`.\n",
        "one/mod.py": HANDLER_MODULE,
        "two/mod.py": HANDLER_MODULE,
    }

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_symbol_in_a_non_python_file_is_checked_by_name(tmp_path: Path) -> None:
    files = {
        "docs/a.md": "See `framer.ts:NewlineFramer`.\n",
        "framer.ts": "export class NewlineFramer {}\n",
    }

    assert _reasons(tmp_path, "docs/a.md", files) == []


def test_missing_symbol_in_a_non_python_file_is_reported(tmp_path: Path) -> None:
    files = {
        "docs/a.md": "See `framer.ts:NewlineFramer`.\n",
        "framer.ts": "export class OtherFramer {}\n",
    }

    assert _reasons(tmp_path, "docs/a.md", files) == [
        "`framer.ts` does not mention `NewlineFramer`"
    ]


def test_prose_that_merely_names_a_file_is_not_a_citation() -> None:
    """The checker must stay usable in docs that talk about files in passing."""
    source = (
        "The models live in `protocol.py`, every one sets `extra='forbid'`, and the\n"
        'gateway listens on `127.0.0.1:8765` (see `type: "protocol_error"`).\n'
    )

    assert _citations(source) == []


def test_citations_inside_code_fences_are_ignored() -> None:
    source = "# A\n\n```\nSee `mod.py:5-7`.\n```\n"

    assert _citations(source) == []


def test_every_gated_doc_resolves() -> None:
    """The gate itself, at the lowest layer: the swept docs stay swept."""
    docs = [REPO_ROOT / doc for doc in GATED_DOCS]
    cited = [
        citation
        for doc in docs
        for citation in extract_citations(doc, doc.read_text(encoding="utf-8"))
    ]

    problems = check(docs, REPO_ROOT, tracked_paths(REPO_ROOT))

    assert [f"`{problem.citation.text}`: {problem.reason}" for problem in problems] == []
    assert cited, "a gated doc with no citations at all means the sweep stopped happening"


def test_a_gated_doc_that_no_longer_exists_is_reported() -> None:
    """A gate that quietly checks nothing is the failure mode to avoid."""
    assert stale_gate_entries(()) == list(GATED_DOCS)
    assert stale_gate_entries(tuple(GATED_DOCS)) == []


@given(name=IDENTIFIERS, other=IDENTIFIERS)
def test_a_python_definition_resolves_and_a_different_name_does_not(name: str, other: str) -> None:
    definitions = python_definitions(f"def {name}() -> None:\n    pass\n")

    assert name in definitions
    assert (other in definitions) == (other == name)


@given(start=LINE_NUMBERS, end=LINE_NUMBERS)
def test_no_line_locator_ever_resolves(start: int, end: int) -> None:
    """Totality is the point: there is no line range the checker accepts."""
    for locator in (str(start), f"{start}-{end}"):
        citation = Citation(
            source=Path("docs/a.md"),
            line=1,
            text=f"mod.py:{locator}",
            path="mod.py",
            locator=locator,
        )

        reason = unresolved_reason(citation, ("mod.py",), Path("/nonexistent"))

        assert reason is not None
        assert "cites a line number" in reason


@given(span=st.text(alphabet=st.characters(blacklist_characters="`:"), max_size=40))
def test_a_code_span_without_a_colon_is_never_a_citation(span: str) -> None:
    assert _citations(f"Text `{span}` more.\n") == []
