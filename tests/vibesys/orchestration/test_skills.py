"""Production ``RunHost.skills`` adapter over the installed skill catalog."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.request import RunRequest
from vibesys.profilers import ProfilerKind
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import RunHost, SkillCatalogError, SkillResolution, SkillResourceRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


_PLUGIN = capability_plugin("skills-test")


def _write_project(root: Path, skill_root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["true"]\n'
    )
    skill = skill_root / "profiling"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: profiling\ndescription: Profiling guidance.\n---\nUse this skill.\n"
    )
    (skill / "references" / "guide.md").write_text("Profiler guide.\n")


def _request(project_root: Path, skill_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="skills-test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "skills-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="skills-test",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
        skills_dirs=[str(skill_root)],
    )


def _run[T](
    tmp_path: Path,
    body: Callable[[RunHost], Awaitable[T]],
) -> T:
    project_root = tmp_path / "project"
    skill_root = tmp_path / "skill-sources"
    _write_project(project_root, skill_root)
    integration = LocalRunIntegration()

    async def exercise() -> T:
        async with open_product_run_host(
            _request(project_root, skill_root),
            integration,
            plugin=_PLUGIN,
        ) as ctx:
            return await body(ctx)

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


def test_skills_resolve_uses_installed_catalog_and_preserves_partial_success(
    tmp_path: Path,
) -> None:
    async def body(ctx: RunHost) -> SkillResolution:
        return await ctx.skills.resolve(
            (
                SkillResourceRequest(
                    name="profiling",
                    resource_paths=("references/guide.md", "missing.md"),
                    purpose="inspect profiler behavior",
                ),
                SkillResourceRequest(name="unknown", purpose="not installed"),
            )
        )

    result = _run(tmp_path, body)

    assert len(result.resolved) == 1
    resolved = result.resolved[0]
    assert resolved.name == "profiling"
    assert resolved.router_path == "profiling/SKILL.md"
    assert resolved.resource_paths == ("profiling/references/guide.md",)
    assert resolved.purpose == "inspect profiler behavior"
    assert len(result.diagnostics) == 2
    assert "resource file does not exist" in result.diagnostics[0]
    assert "unknown installed skill" in result.diagnostics[1]


def test_skills_invalid_catalog_has_a_typed_failure(tmp_path: Path) -> None:
    async def body(ctx: RunHost) -> None:
        skill_md = tmp_path / "skill-sources" / "profiling" / "SKILL.md"
        skill_md.write_text("missing frontmatter\n")
        with pytest.raises(SkillCatalogError, match="SkillMetadataError:"):
            await ctx.skills.resolve(
                (SkillResourceRequest(name="profiling", purpose="inspect profiler behavior"),)
            )

    _run(tmp_path, body)
