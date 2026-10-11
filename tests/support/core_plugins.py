"""Core plugins for tests of the product's host, session and launch wiring.

A test of the wiring around a run (registration, launch, projection, resource lifetime)
needs a plugin that composes and ends, not a strategy that searches. These plugins carry
a declarative core policy over the scripted skeleton strategy: one that stops at once with
a failure, and one that is composed but never run.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict
from tests.support.skeleton_strategy import SkeletonState, SkeletonStrategy

from vibesys.run.evaluation_identity import semantic_evaluation_identity
from vs_core.api import (
    LifecycleClass,
    Limits,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    RunFacts,
    SchemaRef,
    Value,
)
from vs_runtime.api import (
    AgentRole,
    CoreOperation,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    OrchestrationPlugin,
)
from vs_runtime.api.core import OperationRole

if TYPE_CHECKING:
    from vs_core.api import Strategy

_TEMPLATES = Path(__file__).parent / "data"


class EmptyOptions(BaseModel):
    """No configuration for a wiring-only test plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _Rendered(Value):
    status: Literal["succeeded", "failed"] = "succeeded"


class _Render(OperationRequest):
    kind: Literal["test.render"] = "test.render"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[Value]] = _Rendered
    subject: str


def render_registration() -> OperationRegistration:
    """The one operation every test core policy registers."""
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind="test.render",
            request_schema=SchemaRef(name="test-render", version=1),
            outcome_schema=SchemaRef(name="test-render-outcome", version=1),
            lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
            inspect=True,
            cancel=False,
        ),
        request_model=_Render,
        outcome_model=_Rendered,
    )


def core_policy(
    templates: Path = _TEMPLATES,
    strategy: Strategy[Any] | None = None,
    deadline_seconds: float = 1000.0,
) -> CorePolicy:
    """A core policy that plays ``strategy`` (the scripted skeleton by default)."""

    def plan(context: CoreRunContext) -> CorePlan:
        identity = semantic_evaluation_identity(
            context.evaluation_plan, context.facts, context.environment
        )
        return CorePlan(
            strategy=SkeletonStrategy() if strategy is None else strategy,
            reply_schemas={},
            facts=RunFacts(
                objective=context.facts.objective,
                baseline=context.baseline,
                evaluator_digest=identity.evaluator.value,
                workload_digest=identity.workload.value,
                environment_digest=identity.environment.value,
            ),
            limits=Limits(),
            deadline_seconds=deadline_seconds,
        )

    return CorePolicy(
        plan=plan,
        operations=(CoreOperation(OperationRole.RENDER_ARTIFACTS, render_registration()),),
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
    strategy: Strategy[Any] | None = None,
) -> OrchestrationPlugin:
    """A core plugin for a test that composes the host and drives the strategy it is given."""
    policy = core_policy(strategy=strategy)
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
    state = SkeletonState(schema_version=1, phase="failed", failure="scripted")
    return capability_plugin(
        plugin_id,
        agents=agents,
        options=options,
        config_version=config_version,
        artifact_directories=artifact_directories,
        strategy=SkeletonStrategy(state=state, measured=False, keeps_candidate=False),
    )


__all__ = [
    "EmptyOptions",
    "capability_plugin",
    "core_policy",
    "failing_plugin",
    "render_registration",
]
