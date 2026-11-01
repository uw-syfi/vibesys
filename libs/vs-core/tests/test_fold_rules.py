"""A cached fold names the rules that made it, and only those rules read it back.

``state.pb`` is a cache of the fold of a log, and a fold is a function of the log's bytes and of the
code that folds them. A cache keyed by the bytes alone served the state an older reducer made to a
process whose reducers had changed, and the records after it were folded by the new rules over a
state the new rules would not have made. The identity of the rules is a digest of ``vs_core``'s source.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core
import vs_core.api as core
from vs_core.api.recovery import (
    Folded,
    FoldRulesChangedError,
    fold_rules,
    package_sources,
    rules_digest,
)

if TYPE_CHECKING:
    import random

PATHS = st.text(alphabet="abc/._", min_size=1, max_size=6)
SOURCES = st.dictionaries(PATHS, st.binary(max_size=12), min_size=1, max_size=5)


@given(sources=SOURCES, which=st.integers(0, 4))
@example(sources={"a.py": b"x = 1"}, which=0)
def test_the_identity_of_the_rules_changes_with_any_byte_of_any_file(
    sources: dict[str, bytes], which: int
) -> None:
    path = sorted(sources)[which % len(sources)]
    changed = {**sources, path: sources[path] + b"\n"}
    assert rules_digest(changed) != rules_digest(sources)


@given(sources=SOURCES, data=st.data())
def test_the_identity_of_the_rules_changes_with_the_name_of_any_file(
    sources: dict[str, bytes], data: st.DataObject
) -> None:
    path = data.draw(st.sampled_from(sorted(sources)))
    moved = {f"moved/{key}" if key == path else key: value for key, value in sources.items()}
    assert rules_digest(moved) != rules_digest(sources)


@given(sources=SOURCES, seed=st.randoms())
def test_the_identity_of_the_rules_does_not_depend_on_the_order_the_files_were_read_in(
    sources: dict[str, bytes], seed: random.Random
) -> None:
    items = list(sources.items())
    seed.shuffle(items)
    assert rules_digest(dict(items)) == rules_digest(sources)


def test_rules_with_no_source_have_no_identity() -> None:
    assert rules_digest({}) == ""


def test_the_running_rules_are_the_digest_of_every_source_file_of_core_that_is_not_test_support() -> (
    None
):
    root = Path(vs_core.__file__).parent
    expected = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "testing" not in path.relative_to(root).parts
    }
    sources = package_sources()
    assert set(sources) == expected
    assert "_decide.py" in sources
    assert fold_rules() == rules_digest(sources) != ""


def folded() -> Folded:
    return Folded(core.initial_state())


@given(rules=st.text(alphabet="abc", min_size=1, max_size=3))
def test_a_fold_is_read_back_by_the_rules_that_made_it(rules: str) -> None:
    assert Folded.decode(folded().encode(rules), None, rules) == folded()


@given(
    written=st.text(alphabet="abc", max_size=3),
    reading=st.text(alphabet="abc", max_size=3),
)
@example(written="a", reading="b")
@example(written="", reading="")
def test_a_fold_is_refused_by_other_rules_and_by_unknown_ones(written: str, reading: str) -> None:
    if written == reading and reading:
        return
    with pytest.raises(FoldRulesChangedError):
        Folded.decode(folded().encode(written), None, reading)


def test_a_fold_made_by_this_code_is_read_by_this_code() -> None:
    assert Folded.decode(folded().encode()) == folded()
