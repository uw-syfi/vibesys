"""The private-terms check reports where a listed term appears and never what it matched.

The terms here are made up. The real list is private by design and lives outside the
repository, so these tests drive the check's public functions with a terms text of their own.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.check_private_terms import BadTermError, added_lines, findings, parse_terms

TERMS = "example-secret-host\n# a comment\n\n/srv/example-private/\\w+\n"
FILLER = st.text(st.sampled_from("abz ./-_0"), max_size=12).filter(lambda s: "\n" not in s)
SECRETS = st.sampled_from(["example-secret-host", "/srv/example-private/data"])


def _scan(
    *,
    files: list[tuple[str, str]] | None = None,
    diffs: list[str] | None = None,
    messages: list[tuple[str, str]] | None = None,
) -> list[str]:
    return findings(
        parse_terms(TERMS), files=files or [], diffs=diffs or [], messages=messages or []
    )


@given(before=FILLER, after=FILLER, secret=SECRETS, recase=st.sampled_from([str.lower, str.upper]))
def test_a_term_in_a_file_is_reported_by_line_without_its_text(
    before: str, after: str, secret: str, recase: type[str]
) -> None:
    text = f"clean\n{before}{recase(secret)}{after}\nclean\n"

    assert _scan(files=[("docs/a.md", text)]) == ["docs/a.md:2"]


@given(lines=st.lists(FILLER, max_size=6))
def test_text_without_a_term_passes(lines: list[str]) -> None:
    assert _scan(files=[("a.txt", "\n".join(lines))]) == []


@given(secret=SECRETS)
def test_no_finding_contains_the_matched_text(secret: str) -> None:
    diff = f"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+{secret}\n"
    found = _scan(files=[("f.txt", secret)], diffs=[diff], messages=[("a" * 40, f"fix {secret}")])

    assert found
    assert all(secret.lower() not in item.lower() for item in found)


def test_blank_lines_and_comments_in_the_terms_file_match_nothing() -> None:
    assert len(parse_terms(TERMS)) == 2
    assert (
        findings(
            parse_terms("\n# only a comment\n"), files=[("a", "anything")], diffs=[], messages=[]
        )
        == []
    )


def test_a_bad_pattern_is_rejected_by_line_number_without_the_pattern() -> None:
    with pytest.raises(BadTermError) as raised:
        parse_terms("fine\n(unclosed-example-secret\n")

    assert "line 2" in str(raised.value)
    assert "example-secret" not in str(raised.value)


def test_added_lines_carry_new_file_line_numbers_across_files_and_hunks() -> None:
    diff = (
        "diff --git a/one b/one\n--- a/one\n+++ b/one\n"
        "@@ -1,2 +1,3 @@\n+first\n+++ looks like a header\n@@ -9 +20 @@\n+second\n"
        "diff --git a/gone b/gone\n--- a/gone\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n"
    )

    assert [(a.path, a.line, a.text) for a in added_lines(diff)] == [
        ("one", 1, "first"),
        ("one", 2, "++ looks like a header"),
        ("one", 20, "second"),
    ]


def test_a_term_added_in_an_outgoing_commit_and_removed_again_is_still_reported() -> None:
    added = "diff --git a/n b/n\n--- /dev/null\n+++ b/n\n@@ -0,0 +1 @@\n+use example-secret-host\n"
    removed = (
        "diff --git a/n b/n\n--- a/n\n+++ /dev/null\n@@ -1 +0,0 @@\n-use example-secret-host\n"
    )

    assert _scan(diffs=[added + removed]) == ["n:1"]


def test_a_term_in_a_commit_message_is_reported_by_short_sha() -> None:
    assert _scan(messages=[("0123456789abcdef" * 2 + "01234567", "scrub example-secret-host")]) == [
        "commit 0123456789ab message"
    ]
