"""Publish retained trees without borrowing mutation authority over a checkout."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_project._run_branches import adoption_branch, adoption_revision_ref, run_branch
from vs_project.api.git_repository import GitError

if TYPE_CHECKING:
    from vs_project.api.git_repository import GitRepository


@dataclass(frozen=True)
class PublishedRevision:
    """A source revision durably published on a run-owned branch."""

    revision: str
    commit: str
    branch: str


class CheckoutOutcome(StrEnum):
    """Result of the optional checkout update; every refusal preserves publication."""

    FAST_FORWARDED = "fast_forwarded"
    OTHER_BRANCH = "other_branch"
    DIRTY_TREE = "dirty_tree"
    DIVERGED = "diverged"
    GIT_REFUSED = "git_refused"


class PublicationError(RuntimeError):
    """Git refused to publish without forcing a ref or changing a checkout."""

    def __init__(self, branch: str) -> None:
        super().__init__(f"cannot fast-forward publication branch {branch!r}; candidate retained")


class RevisionPublisher:
    """One run's idempotent publication and independent optional checkout update."""

    def __init__(
        self,
        repository: GitRepository,
        run_id: str,
        metadata_path: str,
        preserve_paths: tuple[str, ...] = (),
    ) -> None:
        self._git = repository
        self._branch = adoption_branch(run_id)
        self._run_branch = run_branch(run_id)
        self._run_id = run_id
        self._metadata = metadata_path
        self._preserve_paths = tuple(dict.fromkeys((metadata_path, *preserve_paths)))

    def publish(self, revision: str, message: str) -> PublishedRevision:
        """Record before advancing the branch, so interrupted publication reuses its commit."""
        source = self._git.resolve_commit(revision)
        tip = self._git.resolve_commit(f"refs/heads/{self._run_branch}")
        if source is None or tip is None:
            raise PublicationError(self._branch)
        receipt = adoption_revision_ref(self._run_id, source)
        publication = self._git.resolve_commit(receipt)
        previous = self._git.resolve_commit(f"refs/heads/{self._branch}")
        if publication is None:
            parents = tuple(dict.fromkeys(p for p in (previous, tip) if p is not None))
            publication = self._git.create_commit(
                source,
                parents,
                message,
                preserve_from=tip,
                preserve_paths=self._preserve_paths,
            )
            # The immutable source receipt is also a reachability root. A crash before
            # advancing the branch retries the same commit instead of creating another.
            publication = self._git.create_ref(receipt, publication)
        if (
            previous is None or not self._git.is_ancestor(publication, previous)
        ) and not self._git.advance_branch(self._branch, publication):
            raise PublicationError(self._branch)
        return PublishedRevision(source, publication, self._branch)

    def update_checkout(self, publication: PublishedRevision) -> CheckoutOutcome:
        """Let Git enforce overwrite protection, including ignored-file conflicts."""
        receipt = adoption_revision_ref(self._run_id, publication.revision)
        if (
            publication.branch != self._branch
            or self._git.resolve_commit(receipt) != publication.commit
        ):
            raise PublicationError(self._branch)
        try:
            return self._update_checkout(publication)
        except (GitError, OSError):
            return CheckoutOutcome.GIT_REFUSED

    def _update_checkout(self, publication: PublishedRevision) -> CheckoutOutcome:
        if self._git.current_branch() != self._run_branch:
            return CheckoutOutcome.OTHER_BRANCH
        if self._git.uncommitted_paths(
            [".", f":(exclude){self._metadata}"]
        ) or self._git.tracked_changes_since_head([self._metadata]):
            return CheckoutOutcome.DIRTY_TREE
        head = self._git.head()
        if head is None or not self._git.is_ancestor(head, publication.commit):
            return CheckoutOutcome.DIVERGED
        if not self._git.fast_forward_checkout(
            self._run_branch,
            publication.commit,
            untracked_exclusions=(self._metadata,),
        ):
            return CheckoutOutcome.GIT_REFUSED
        return CheckoutOutcome.FAST_FORWARDED
