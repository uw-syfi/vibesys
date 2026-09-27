"""Production adapter for resolving selected resources from installed skills."""

# lint-waiver: LW-040120 [SLF001]; sibling host capabilities share run-owned resources while the migration adapter exists.
# ruff: noqa: SLF001

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

    from vibesys.orchestration._host import HostResources


class _Skills:
    """Resolve policy-owned recommendations against this run's installed catalog."""

    def __init__(self, host: HostResources) -> None:
        self._host = host

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Return partial valid selections, diagnostics, or a catalog failure."""
        if not requests:
            return SkillResolution()
        sources = tuple(self._host._resources.skill_source_paths)
        if not sources:
            return SkillResolution(diagnostics=("no skill sources are installed",))
        try:
            resolution = await self._host._run_blocking(self._resolve, requests, sources)
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
