"""The stray-file check flags exactly the paths whose name has a tool's leftover suffix."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from scripts.check_stray_files import STRAY_SUFFIXES, stray_files

_NAMES = st.text(st.sampled_from("abz._-/"), min_size=1, max_size=12).filter(
    lambda name: not name.endswith("/")
)


@given(
    clean=st.lists(_NAMES.filter(lambda name: not name.endswith(STRAY_SUFFIXES)), max_size=5),
    stray=st.lists(st.tuples(_NAMES, st.sampled_from(STRAY_SUFFIXES)), max_size=3),
)
def test_only_names_ending_in_a_leftover_suffix_are_flagged(
    clean: list[str], stray: list[tuple[str, str]]
) -> None:
    leftovers = [name + suffix for name, suffix in stray]

    assert stray_files([*clean, *leftovers]) == sorted(leftovers)


def test_the_sed_backup_from_1915_is_flagged_and_its_original_is_not() -> None:
    assert stray_files(["src/server/host_helper.py", "src/server/host_helper.py-e"]) == [
        "src/server/host_helper.py-e"
    ]
