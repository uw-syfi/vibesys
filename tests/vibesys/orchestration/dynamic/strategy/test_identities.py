"""Strategy identities encode any agent-chosen subject as one whitespace-free component."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.dynamic.strategy.api import decision_id, invocation_id, session_id

_SUBJECTS = st.text(max_size=40).filter(bool)


@given(first=_SUBJECTS, second=_SUBJECTS)
def test_identities_accept_any_subject_and_keep_distinct_subjects_distinct(
    first: str, second: str
) -> None:
    for build in (
        lambda subject: decision_id("turn", subject, 3).root,
        lambda subject: invocation_id("implementer", subject, 3).root,
        lambda subject: session_id("implementer", subject).root,
    ):
        built = build(first)
        assert built.split() == [built]
        assert (build(first) == build(second)) == (first == second)


@given(subject=_SUBJECTS)
def test_the_subject_is_one_component_whatever_separators_it_holds(subject: str) -> None:
    root = invocation_id("judge", subject, 1).root
    # inv:<role>:<subject>:<serial>: the subject adds no separator of its own.
    assert root.count(":") == 3
