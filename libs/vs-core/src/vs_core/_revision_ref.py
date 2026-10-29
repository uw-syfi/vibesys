"""Git revision construction and inspection, independent of representation validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, cast

from vs_core._corevalue import ContractValidationError
from vs_core._generated.vibesys.core.v1.common_cv import RevisionId
from vs_core._generated.vibesys.core.v1.vocabulary_cv import DigestScheme

if TYPE_CHECKING:
    from collections.abc import Callable


class RevisionReference:
    """Canonical construction and recognition of git-commit revision references."""

    __slots__ = ()
    revision_id: RevisionId
    digest: str

    @classmethod
    def of_git_commit(cls, commit: str) -> Self:
        """Mint a revision reference from a bare git commit identity."""
        if not commit or commit != commit.strip() or ":" in commit:
            raise ContractValidationError("commit", "must be a bare commit id")
        constructor = cast("Callable[..., Self]", cls)
        return constructor(
            revision_id=RevisionId(root=commit), digest=f"{DigestScheme.GIT_COMMIT}:{commit}"
        )

    @property
    def git_commit(self) -> str | None:
        """Return the commit only if the identity and digest agree."""
        prefix = f"{DigestScheme.GIT_COMMIT}:"
        if not self.digest.startswith(prefix):
            return None
        commit = self.digest.removeprefix(prefix)
        return commit if commit and commit == self.revision_id.root else None
