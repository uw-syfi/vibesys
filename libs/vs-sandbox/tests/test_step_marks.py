"""The mark a step's commands carry, and the argv that puts it on the group leader."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import STEP_TAG_ENV, step_shell_argv, step_tag_env

safe = st.text(
    alphabet=st.characters(blacklist_characters="\n\0 \t", blacklist_categories=("Cs",)),
    min_size=1,
    max_size=20,
)
commands = st.text(min_size=1, max_size=40)


@given(shell=safe, command=commands)
def test_an_unmarked_command_runs_under_the_plain_shell(shell: str, command: str) -> None:
    assert step_shell_argv(shell, command, {}) == (shell, "-c", command)


@given(shell=safe, command=commands, tag=safe)
def test_a_marked_command_is_passed_through_whole_under_a_leader_that_names_the_mark(
    shell: str, command: str, tag: str
) -> None:
    argv = step_shell_argv(shell, command, step_tag_env(tag))
    assert argv[-1] == command
    assert argv[-2] == shell
    assert f"{STEP_TAG_ENV}={tag}" in argv


@pytest.mark.parametrize("tag", ["", "a b", "a\tb", "a\nb", "a\0b"])
def test_a_tag_that_cannot_be_matched_exactly_is_refused(tag: str) -> None:
    with pytest.raises(ValueError, match="step tag"):
        step_tag_env(tag)
