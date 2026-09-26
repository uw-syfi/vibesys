"""Production adapter for resolving selected resources from installed skills."""

# lint-waiver: LW-040120 [SLF001]; sibling host capabilities share run-owned resources while the migration adapter exists.
# ruff: noqa: SLF001

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.schemas import SkillResourceSelection as PolicySkillResourceSelection
from vibesys.skills import ResolvedSkillSelection, build_skill_catalog, resolve_skill_selections
from vs_runtime.api import (
    ResolvedSkillResources,
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
)

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
            resolved, diagnostics = await self._host._run_blocking(self._resolve, requests, sources)
        except (OSError, ValueError) as error:
            detail = f"{type(error).__name__}: {error}"
            raise SkillCatalogError(detail) from error
        return SkillResolution(
            resolved=tuple(
                ResolvedSkillResources(
                    name=selection.skill,
                    router_path=selection.router_path,
                    resource_paths=selection.resource_paths,
                    purpose=selection.purpose,
                )
                for selection in resolved
            ),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _resolve(
        requests: tuple[SkillResourceRequest, ...], sources: tuple[Path, ...]
    ) -> tuple[list[ResolvedSkillSelection], list[str]]:
        """Adapt neutral requests to VibeSys's catalog and validation policy."""
        catalog = build_skill_catalog(sources)
        policy_requests = [
            PolicySkillResourceSelection(
                skill=request.name,
                resource_paths=list(request.resource_paths),
                purpose=request.purpose,
            )
            for request in requests
        ]
        return resolve_skill_selections(policy_requests, catalog)


__all__ = ["_Skills"]
