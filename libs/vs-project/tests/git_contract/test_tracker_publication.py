"""Project's checkout proof protects operator content across generated Git histories."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from vs_project.api import CheckoutOutcome, GitTracker, NullGitTrackerEvents
from vs_project.api.testing import ContractProject
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import PublishedRevision
    from vs_project.api.testing import RepositoryFactory


def _start(project: ContractProject) -> GitTracker:
    project.start({"code.py": "input", ".gitignore": "ignored\n"})
    tracker = GitTracker(
        project.root, run_id="run", events=NullGitTrackerEvents(), repository=project.repo
    )
    tracker.init(existing=False)
    return tracker


def _candidate(project: ContractProject, index: int) -> str:
    baseline = project.repo.head()
    assert baseline is not None
    path = project.root.parent / f"{project.root.name}-candidate-{index}"
    project.repo.add_worktree(path, baseline)
    candidate = ContractProject(path, project.factory)
    candidate.write("code.py", f"winner {index}")
    candidate.write("added", f"winner added {index}")
    candidate.write("ignored", f"winner ignored {index}")
    candidate.repo.stage_all(["."], force=True)
    candidate.repo.commit("candidate")
    revision = candidate.repo.head()
    assert revision is not None
    return revision


@pytest.mark.regression
def test_committed_operator_edit_before_publication_is_never_replaced(
    sandbox: ContractProject,
) -> None:
    tracker = _start(sandbox)
    revision = _candidate(sandbox, 0)
    tracker.retain_candidate("winner", revision)
    sandbox.write("code.py", "operator committed edit")
    operator = sandbox.commit_all("operator edit after retain")
    publication = tracker.publish_revision(revision, "winner")
    outcome = tracker.update_checkout(publication)
    assert (sandbox.root / "code.py").read_text() == "operator committed edit"
    assert sandbox.repo.head() == operator
    assert outcome is CheckoutOutcome.OPERATOR_CHANGED_CHECKOUT
    assert not sandbox.repo.is_ancestor(operator, publication.commit)
    assert sandbox.repo.read_blob(publication.commit, "code.py") == b"winner 0"
    assert sandbox.repo.resolve_commit(publication.branch) == publication.commit


def _stage_operator_edit(project: ContractProject, action: str) -> None:
    if action in {"commit", "staged"}:
        project.repo.stage_all(["code.py"])
        if action == "commit":
            project.repo.commit("operator edit")


_ACTIONS = st.lists(
    st.sampled_from(
        (
            "retain",
            "publish",
            "update",
            "commit",
            "tracked",
            "staged",
            "untracked",
            "ignored",
            "resume",
        )
    ),
    min_size=0,
    max_size=15,
)


@pytest.mark.regression
@settings(
    max_examples=examples(pr=5, nightly=40),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(history=_ACTIONS)
@example(history=["commit", "retain", "publish", "update"])
@example(history=["retain", "commit", "publish", "update"])
@example(history=["publish", "update", "commit", "retain", "publish", "update"])
@example(history=["publish", "commit", "update", "retain", "publish", "update"])
@example(history=["update", "retain", "publish", "update", "retain", "publish", "update"])
@example(history=["publish", "update", "retain", "publish", "retain", "publish", "update"])
@example(history=["ignored", "publish", "update", "untracked", "retain", "publish", "update"])
def test_generated_publication_histories_preserve_operator_content(
    tmp_path: Path, factory: RepositoryFactory, history: list[str]
) -> None:
    root = tmp_path / str(len(list(tmp_path.iterdir())))
    root.mkdir()
    project = ContractProject(root, factory)
    tracker = _start(project)
    baseline = tracker.trusted_input_baseline
    retained = _candidate(project, 0)
    tracker.retain_candidate("initial", retained)
    publication: PublishedRevision | None = None
    protected: dict[str, bytes] = {}
    for index, action in enumerate([*history, "publish", "update"]):
        if action == "retain":
            retained = _candidate(project, index + 1)
            tracker.retain_candidate(f"candidate-{index}", retained)
        elif action == "publish":
            before = project.repo.head()
            publication = tracker.publish_revision(retained, "winner")
            assert project.repo.head() == before
            assert project.repo.read_blob(publication.commit, "code.py") == project.repo.read_blob(
                retained, "code.py"
            )
        elif action == "update" and publication is not None:
            outcome = tracker.update_checkout(publication)
            if outcome is CheckoutOutcome.FAST_FORWARDED:
                assert project.repo.head() == publication.commit
                assert (root / "code.py").read_bytes() == project.repo.read_blob(
                    publication.commit, "code.py"
                )
        elif action == "resume":
            tracker = GitTracker(
                root, run_id="run", events=NullGitTrackerEvents(), repository=project.repo
            )
            tracker.init(existing=True, trusted_input_baseline=baseline)
        elif action in {"commit", "tracked", "staged", "untracked", "ignored"}:
            name = {"untracked": "added", "ignored": "ignored"}.get(action, "code.py")
            content = f"operator {action} {index}"
            project.write(name, content)
            protected[name] = content.encode()
            _stage_operator_edit(project, action)
        assert all((root / name).read_bytes() == value for name, value in protected.items())
    assert publication is not None
    assert project.repo.resolve_commit(publication.branch) == publication.commit
    if not protected:
        assert project.repo.head() == publication.commit
