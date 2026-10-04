"""The revision digest scheme is a typed contract with an exact round trip."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

COMMITS = st.text(alphabet="0123456789abcdef", min_size=7, max_size=64)


@given(commit=COMMITS)
def test_git_commit_refs_round_trip_through_the_wire(commit: str) -> None:
    ref = core.RevisionRef.of_git_commit(commit)
    assert ref.revision_id == core.RevisionId(root=commit)
    assert ref.digest == f"{core.DigestScheme.GIT_COMMIT}:{commit}"
    assert ref.git_commit == commit
    assert core.RevisionRef.model_validate_json(ref.model_dump_json()) == ref


@given(commit=COMMITS, other=COMMITS, scheme=st.sampled_from(["git-commit:", "tree:", ""]))
def test_git_commit_is_none_unless_digest_names_the_same_commit(
    commit: str, other: str, scheme: str
) -> None:
    ref = core.RevisionRef(revision_id=core.RevisionId(root=commit), digest=f"{scheme}{other}")
    expected = commit if scheme == "git-commit:" and other == commit else None
    assert ref.git_commit == expected


@pytest.mark.parametrize("commit", ["", " abc", "abc ", "git-commit:abc"])
def test_of_git_commit_rejects_non_bare_commit_ids(commit: str) -> None:
    with pytest.raises(core.ContractValidationError):
        core.RevisionRef.of_git_commit(commit)
