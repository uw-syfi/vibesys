"""Any canonical agent id names a workspace that is safe as a path component, a state namespace and a Git ref.

An id an agent chose becomes the name of its candidate workspace, which is a directory, a project
state namespace and the last component of ``refs/vibesys/<run>/candidates/<id>``. The name is
derived from the id once; these properties hold for every canonical id, whatever characters it
holds, and two ids never share a name.
"""

from __future__ import annotations

import re
import unicodedata

from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import member_workspace_id

CANONICAL = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(lambda value: value == value.strip() and unicodedata.is_normalized("NFC", value))
"""Every spelling an agent may give an id (see ``AgentId``): printable, NFC, trimmed, 1 to 128."""

_NAME = re.compile(r"m-([a-z0-9._-]*-)?[0-9a-f]{12}")
_GIT_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


@given(identifier=CANONICAL)
def test_a_workspace_name_is_a_plain_path_component_and_a_legal_git_ref_component(
    identifier: str,
) -> None:
    name = member_workspace_id(identifier)

    assert _NAME.fullmatch(name), name
    assert len(name) <= 63
    # The rules of ``git check-ref-format`` for one ref component.
    assert not _GIT_FORBIDDEN.search(name)
    assert not name.startswith(".")
    assert not name.endswith((".", ".lock"))
    assert "/" not in name


@given(first=CANONICAL, second=CANONICAL)
def test_two_different_ids_never_share_a_workspace_name(first: str, second: str) -> None:
    assert (member_workspace_id(first) == member_workspace_id(second)) == (first == second)


@given(identifier=CANONICAL)
def test_ids_that_differ_only_in_case_or_punctuation_get_different_names(identifier: str) -> None:
    variants = {
        identifier,
        identifier.upper(),
        identifier.lower(),
        identifier + ".",
        identifier + "-",
    }

    assert len({member_workspace_id(item) for item in variants}) == len(variants)
