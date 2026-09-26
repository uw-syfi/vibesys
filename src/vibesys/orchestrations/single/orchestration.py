"""Ordinary async control flow for the first single-agent policy slice.

This slice owns agent prompts, semantic plan correction, and implementation
retry decisions. It deliberately does not claim to replace the current loop:
candidate workspaces, durable state, trusted evaluation, and cancellation are
not yet capabilities of :mod:`vs_runtime.api`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestrations.single.agents import DESIGNER, IMPLEMENTER
from vibesys.orchestrations.single.models import (
    InvalidSinglePlanError,
    SingleAgentResult,
    SingleOptions,
    SinglePlan,
    Verdict,
    fallback_plan,
    fallback_result,
)
from vibesys.orchestrations.single.prompts import (
    implementation_message,
    plan_correction_message,
    plan_message,
    revision_message,
)
from vs_runtime.api import RunHost, RunStatus, StructuredResponseError, Workspace

if TYPE_CHECKING:
    from pydantic import BaseModel


def _validate_plan(plan: SinglePlan) -> None:
    update_ids = [update.hypothesis_id for update in plan.hypothesis_updates]
    if len(update_ids) != len(set(update_ids)):
        raise InvalidSinglePlanError.duplicate_updates()
    if plan.hypothesis_id in update_ids:
        raise InvalidSinglePlanError.self_update()


async def _select_plan(host: RunHost, options: SingleOptions, workspace: Workspace) -> SinglePlan:
    designer = await host.agents.create_session(DESIGNER, workspace=workspace)
    try:
        try:
            plan = await designer.turn(plan_message(options), response=SinglePlan)
        except StructuredResponseError:
            host.log("designer returned no structured plan; using the policy fallback")
            return fallback_plan()
        try:
            _validate_plan(plan)
        except InvalidSinglePlanError as error:
            host.log(f"designer plan rejected: {error}; requesting one correction")
            plan = await designer.turn(plan_correction_message(plan, error), response=SinglePlan)
            _validate_plan(plan)
        return plan
    finally:
        await designer.close()


async def _implement_plan(
    host: RunHost,
    options: SingleOptions,
    workspace: Workspace,
    plan: SinglePlan,
) -> RunStatus:
    implementer = await host.agents.create_session(
        IMPLEMENTER,
        workspace=workspace,
    )
    try:
        message = implementation_message(plan)
        for attempt in range(options.max_retries_per_round + 1):
            try:
                result = await implementer.turn(message, response=SingleAgentResult)
            except StructuredResponseError:
                host.log("implementer returned no structured result; applying fallback")
                result = fallback_result()
            if result.verdict is Verdict.APPROVE:
                host.log(f"implementation passed on attempt {attempt + 1}")
                return RunStatus.SUCCEEDED
            if attempt < options.max_retries_per_round:
                host.log(f"implementation rejected on attempt {attempt + 1}; retrying")
                message = revision_message(result)
        host.log("implementation retry budget exhausted")
        return RunStatus.FAILED
    finally:
        await implementer.close()


async def orchestrate(host: RunHost, raw_options: BaseModel) -> RunStatus:
    """Run the bounded agent-policy slice against explicit runtime sessions."""
    options = SingleOptions.model_validate(raw_options)
    workspace = host.workspaces.root
    plan = await _select_plan(host, options, workspace)
    return await _implement_plan(host, options, workspace, plan)


__all__ = ["orchestrate"]
