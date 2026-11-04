"""Where the run's steer text lives: its content-addressed artifact store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_core.api import ArtifactRef
    from vs_runtime.api import ArtifactStore


class StoredSteerArtifacts:
    """Where steer text lives: the run's content-addressed artifact store.

    A steer control carries only the digest of its text, so the text must be readable by
    that digest before the control is submitted.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        """Store steer text in the run's content-addressed artifacts."""
        self._artifacts = artifacts

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text; its content address must be the digest the control carries."""
        receipt = self._artifacts.write(text.encode())
        if receipt.sha256 != ref.digest:
            message = f"steer artifact {ref.artifact_id.root!r} stored under {receipt.sha256}"
            raise RuntimeError(message)
