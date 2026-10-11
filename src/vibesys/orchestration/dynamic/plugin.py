"""Plugin registration for dynamic portfolio hypothesis search."""

from collections.abc import Sequence
from functools import partial

from pydantic import BaseModel

from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.dynamic.agents import AGENTS
from vibesys.orchestration.dynamic.models import DynamicOptions, DynamicState, DynamicWorkstream
from vibesys.orchestration.dynamic.orchestration import orchestrate
from vibesys.orchestration.resume import compare_round_budget
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import PluginProjection, WorkstreamAttemptSummary
from vs_runtime.api import OrchestrationPlugin

# Mirrors the tail-slicing convention this codebase already uses to bound
# long diagnostic text on an event payload (``GATE_LOG_TAIL_CHARS`` in
# ``run/evaluation.py``), scoped to this module since the two truncate
# unrelated text (gate output vs. a workstream's failure message).
_WORKSTREAM_ERROR_TAIL_CHARS = 1000


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the authoritative shared hypothesis state plus live workstreams."""
    state = DynamicState.model_validate(raw_state)
    projection = project_hypothesis_state(state.search)
    return projection.model_copy(update={"workstreams": _workstream_summaries(state.workstreams)})


def _workstream_summaries(
    workstreams: Sequence[DynamicWorkstream],
) -> tuple[WorkstreamAttemptSummary, ...]:
    """Project each workstream's live retry-budget diagnostics."""
    return tuple(
        WorkstreamAttemptSummary(
            hypothesis_id=item.hypothesis_id,
            attempt=item.budget.spent,
            attempt_limit=item.retry_limit,
            refunded=item.budget.refunded,
            last_error=(
                None if item.last_error is None else item.last_error[-_WORKSTREAM_ERROR_TAIL_CHARS:]
            ),
            setup_failure=item.setup_failure,
        )
        for item in workstreams
    )


def _project_max_rounds(raw_options: BaseModel) -> int:
    """Project the round budget: every workstream records one round."""
    options = DynamicOptions.model_validate(raw_options)
    return options.max_rounds * options.max_in_flight


PLUGIN = OrchestrationPlugin(
    id="dynamic",
    agents=AGENTS,
    options=DynamicOptions,
    state=DynamicState,
    orchestrate=orchestrate,
)
REGISTRATION = OrchestrationRegistration(
    plugin=PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="dynamic",
        options_type=DynamicOptions,
    ),
    project_max_rounds=_project_max_rounds,
)

__all__ = ["PLUGIN", "REGISTRATION"]
