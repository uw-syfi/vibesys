"""Production adapter for resolving selected resources from installed skills."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime.api import (
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
)
from vs_runtime.api.infrastructure import build_skill_catalog, resolve_skill_resources

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import BlockingOperations


class _Skills:
    """Resolve policy-owned recommendations against this run's installed catalog."""

    def __init__(
        self,
        source_paths: tuple[Path, ...],
        blocking: BlockingOperations,
    ) -> None:
        self._source_paths = source_paths
        self._blocking = blocking

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Return partial valid selections, diagnostics, or a catalog failure."""
        if not requests:
            return SkillResolution()
        if not self._source_paths:
            return SkillResolution(diagnostics=("no skill sources are installed",))
        try:
            resolution = await self._blocking.run(self._resolve, requests, self._source_paths)
        except (OSError, ValueError) as error:
            detail = f"{type(error).__name__}: {error}"
            raise SkillCatalogError(detail) from error
        return resolution

    @staticmethod
    def _resolve(
        requests: tuple[SkillResourceRequest, ...], sources: tuple[Path, ...]
    ) -> SkillResolution:
        """Resolve neutral requests against the installed runtime catalog."""
        return resolve_skill_resources(requests, build_skill_catalog(sources))


__all__ = ["_Skills"]
