"""Publication never needs checkout authority; checkout uses Git's overwrite rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_project.api import GitCommandError

if TYPE_CHECKING:
    from vs_project.api.testing import ContractProject


def _winner(project: ContractProject) -> tuple[str, str]:
    baseline = project.start({"code.py": "input", ".gitignore": "ignored\ncache/\n"})
    project.repo.switch_branch("candidate", create=True)
    project.write("code.py", "winner")
    project.write("added.py", "added")
    project.write("ignored", "candidate tracked content")
    project.repo.stage_all(["."], force=True)
    project.repo.commit("candidate")
    winner = project.repo.head()
    assert winner is not None
    project.repo.switch_branch("main")
    return baseline, winner


@pytest.mark.parametrize("edit", ["none", "tracked", "staged", "untracked", "ignored"])
def test_publish_preserves_checkout_and_optional_fast_forward_protects_edits(
    sandbox: ContractProject,
    edit: str,
) -> None:
    baseline, winner = _winner(sandbox)
    if edit in {"tracked", "staged"}:
        sandbox.write("code.py", "operator edit")
        if edit == "staged":
            sandbox.repo.stage_all(["code.py"])
    elif edit == "untracked":
        sandbox.write("added.py", "operator notes")
    elif edit == "ignored":
        sandbox.write("ignored", "operator cache")
    sandbox.write("cache/unrelated", "keep ignored cache")
    pending = sandbox.repo.uncommitted_paths(())
    staged = sandbox.repo.has_staged_changes()
    published = sandbox.repo.create_commit(
        winner,
        [baseline],
        "publish",
        preserve_from=baseline,
    )
    assert sandbox.repo.advance_branch("published", published)
    assert sandbox.repo.head() == baseline
    assert sandbox.repo.current_branch() == "main"
    assert sandbox.repo.uncommitted_paths(()) == pending
    assert sandbox.repo.has_staged_changes() == staged
    assert sandbox.repo.read_blob(published, "code.py") == b"winner"
    assert not sandbox.repo.advance_branch("main", published)  # checked out
    updated = sandbox.repo.fast_forward_checkout("main", published)
    assert updated is (edit == "none")
    assert (sandbox.root / "cache/unrelated").read_text() == "keep ignored cache"
    if edit == "none":
        assert sandbox.repo.head() == published
        assert sandbox.repo.uncommitted_paths(()) == ()
        assert (sandbox.root / "code.py").read_text() == "winner"
    else:
        assert sandbox.repo.head() == baseline
        assert sandbox.repo.uncommitted_paths(()) == pending
        path = (
            "code.py"
            if edit in {"tracked", "staged"}
            else "added.py"
            if edit == "untracked"
            else "ignored"
        )
        expected = (
            "operator edit"
            if edit in {"tracked", "staged"}
            else "operator notes"
            if edit == "untracked"
            else "operator cache"
        )
        assert (sandbox.root / path).read_text() == expected


def test_publication_preserves_metadata_and_only_advances_branches(
    sandbox: ContractProject,
) -> None:
    baseline, winner = _winner(sandbox)
    sandbox.write("state/current", "current metadata")
    parent = sandbox.commit_all("metadata")
    publication = sandbox.repo.create_commit(
        winner,
        [parent],
        "publish",
        preserve_from=parent,
        preserve_paths=["state"],
    )
    assert sandbox.repo.advance_branch("published", publication)
    assert sandbox.repo.read_blob(publication, "state/current") == b"current metadata"
    assert sandbox.repo.read_blob(publication, "code.py") == b"winner"
    assert not sandbox.repo.advance_branch("published", baseline)
    assert sandbox.repo.resolve_commit("published") == publication
    assert not sandbox.repo.fast_forward_checkout("another-branch", publication)
    assert sandbox.repo.head() == parent


def test_publication_refuses_a_branch_checked_out_in_a_linked_worktree(
    sandbox: ContractProject,
) -> None:
    baseline, winner = _winner(sandbox)
    assert sandbox.repo.advance_branch("published", baseline)
    other_path = sandbox.root.parent / "linked"
    sandbox.repo.add_worktree(other_path, baseline)
    linked = sandbox.factory(other_path)
    linked.bind()
    linked.switch_branch("published")
    publication = sandbox.repo.create_commit(winner, [baseline], "publish", preserve_from=baseline)
    assert not sandbox.repo.advance_branch("published", publication)
    assert linked.head() == baseline
    assert (other_path / "code.py").read_text() == "input"


def test_publication_receipts_are_atomic_first_writer_wins(sandbox: ContractProject) -> None:
    baseline, winner = _winner(sandbox)
    ref = "refs/publication-receipts/source"
    assert sandbox.repo.create_ref(ref, baseline) == baseline
    assert sandbox.repo.create_ref(ref, winner) == baseline
    assert sandbox.repo.resolve_commit(ref) == baseline
    assert sandbox.repo.head() == baseline


def test_create_ref_cannot_move_a_checked_out_branch(sandbox: ContractProject) -> None:
    baseline, winner = _winner(sandbox)
    assert sandbox.repo.create_ref("refs/heads/main", winner) == baseline
    assert sandbox.repo.head() == baseline
    assert (sandbox.root / "code.py").read_text() == "input"


def test_invalid_receipt_inputs_cannot_be_hidden_by_an_existing_ref(
    sandbox: ContractProject,
) -> None:
    baseline, _ = _winner(sandbox)
    ref = "refs/publication-receipts/source"
    assert sandbox.repo.create_ref(ref, baseline) == baseline
    with pytest.raises(GitCommandError):
        sandbox.repo.create_ref(ref, "0" * 40)
    with pytest.raises(GitCommandError):
        sandbox.repo.create_ref("HEAD", baseline)
    assert sandbox.repo.head() == baseline


@pytest.mark.regression
@pytest.mark.parametrize("syntax", ["force", "refspec", "option", "missing"])
def test_revision_syntax_cannot_bypass_publication_safety(
    sandbox: ContractProject,
    syntax: str,
) -> None:
    baseline, winner = _winner(sandbox)
    assert sandbox.repo.advance_branch("published", winner)
    invalid = {
        "force": f"+{baseline}",
        "refspec": f"{baseline}:refs/heads/main",
        "option": "--no-ff",
        "missing": "0" * 40,
    }[syntax]
    assert not sandbox.repo.advance_branch("published", invalid)
    assert sandbox.repo.resolve_commit("published") == winner
    assert not sandbox.repo.fast_forward_checkout("main", invalid)
    assert sandbox.repo.head() == baseline
    assert (sandbox.root / "code.py").read_text() == "input"
