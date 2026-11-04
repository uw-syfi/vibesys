"""Core plugins for tests of the product's host, session and launch wiring.

A test of the wiring around a run (registration, launch, projection, resource lifetime)
needs a plugin that composes and ends, not a strategy that searches. These plugins carry
a declarative core policy over the scripted skeleton strategy: one that stops at once with
a failure, and one that is composed but never run.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from tests.support.skeleton_strategy import OPERATIONS, SkeletonConfig, SkeletonStrategy, skeleton

from vibesys.run.evaluation_identity import semantic_evaluation_identity
from vs_core.api import Limits, RunFacts, SearchDef
from vs_core.api.testing import sample_limits
from vs_runtime.api import (
    AgentRole,
    CoreOperation,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
)
from vs_runtime.api.core import operation_roles

_TEMPLATES = Path(__file__).parent / "data"


class EmptyOptions(BaseModel):
    """No configuration for a wiring-only test plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def core_policy(
    templates: Path = _TEMPLATES,
    strategy: SkeletonStrategy | None = None,
    deadline_seconds: float = 1000.0,
    search: SearchDef[SkeletonConfig] = skeleton,
    limits: Limits | None = None,
) -> CorePolicy:
    """A core policy that plays ``strategy`` (the scripted skeleton by default).

    ``search`` replaces the skeleton's flow for a test that scripts its own, over the same
    operations and config. ``limits`` replaces ``sample_limits()``.
    """
    scenario = SkeletonStrategy() if strategy is None else strategy

    def plan(context: CoreRunContext) -> CorePlan:
        identity = semantic_evaluation_identity(
            context.evaluation_plan, context.facts, context.environment
        )
        return CorePlan(
            search=search,
            config=scenario.config,
            facts=RunFacts(
                objective=context.facts.objective,
                baseline=context.baseline,
                evaluator_digest=identity.evaluator.value,
                workload_digest=identity.workload.value,
                environment_digest=identity.environment.value,
            ),
            limits=sample_limits() if limits is None else limits,
            deadline_seconds=deadline_seconds,
        )

    return CorePolicy(
        plan=plan,
        operations=tuple(
            CoreOperation(role, registration)
            for role, registration in operation_roles(OPERATIONS).items()
        ),
        retention_label="selected",
        prompt_templates=templates,
    )


def capability_plugin(  # noqa: PLR0913  # lint-waiver: LW-950002 [PLR0913]; each keyword is one independent declaration of the plugin under test.
    plugin_id: str,
    *,
    agents: tuple[AgentRole, ...] = (),
    options: type[BaseModel] = EmptyOptions,
    config_version: int = 1,
    artifact_directories: tuple[str, ...] = (),
    strategy: SkeletonStrategy | None = None,
    search: SearchDef[SkeletonConfig] = skeleton,
    deadline_seconds: float = 1000.0,
    limits: Limits | None = None,
) -> OrchestrationPlugin:
    """A core plugin for a test that composes the host and drives the strategy it is given.

    ``deadline_seconds`` is the run's own deadline, counted from its start, and ``limits``
    its core limits (``sample_limits()`` by default).
    """
    policy = core_policy(
        strategy=strategy, search=search, deadline_seconds=deadline_seconds, limits=limits
    )
    if artifact_directories:
        policy = replace(policy, artifact_directories=artifact_directories)
    return OrchestrationPlugin(
        id=plugin_id, agents=agents, options=options, core=policy, config_version=config_version
    )


def failing_plugin(
    plugin_id: str,
    *,
    agents: tuple[AgentRole, ...] = (),
    options: type[BaseModel] = EmptyOptions,
    config_version: int = 1,
    artifact_directories: tuple[str, ...] = (),
) -> OrchestrationPlugin:
    """A core plugin whose run stops at once with a scripted failure, touching no agent."""
    return capability_plugin(
        plugin_id,
        agents=agents,
        options=options,
        config_version=config_version,
        artifact_directories=artifact_directories,
        strategy=SkeletonStrategy.failing(),
    )


__all__ = [
    "EmptyOptions",
    "capability_plugin",
    "core_policy",
    "failing_plugin",
]
