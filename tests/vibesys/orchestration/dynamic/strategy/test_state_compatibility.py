"""Persisted dynamic v1 state remains readable across compatible fixes."""

from __future__ import annotations

from vibesys.orchestration.dynamic.strategy.api import DynamicStrategyState
from vs_core.api import InvocationId, InvocationRef, SessionId

# Serialized by DynamicStrategyState at exact merge base 04295e787bf6b11c6723328a20a2c39ad78b60f9.
_V1_STATE_WITH_TURN_LINEAGE = """
{
  "schema_version": 1,
  "phase": "searching",
  "stopping": false,
  "baseline": {
    "stage": "needed",
    "attempts": 0,
    "awaiting": null,
    "evidence": [],
    "accuracy_passed": null,
    "benchmark_passed": null,
    "metrics": [],
    "partial": null,
    "failure": null
  },
  "planner": {
    "call": 1,
    "step": "needed",
    "active": false,
    "awaiting": null,
    "turn": {
      "serial": 0,
      "corrections": 0,
      "drops": 0,
      "ask_not_before": null,
      "charge": "paid",
      "context": null,
      "prompts": [],
      "tool_policy": null,
      "invocation": null,
      "resume_as": null,
      "continuation": null,
      "yielded": false,
      "lineage": [
        {
          "session_id": {"root": "session", "kind": "session"},
          "invocation_id": {"root": "older", "kind": "invocation"},
          "generation": 0
        }
      ],
      "role": "orchestrator"
    },
    "blocked_at_done": null,
    "capacity": 0,
    "held_plan_json": null,
    "last_error": null,
    "failed": false,
    "retries": 0
  },
  "hypotheses": [],
  "attempts": [],
  "parents": [],
  "winner": null,
  "refunded": 0,
  "withheld": [],
  "unreachable": []
}
"""


def test_exact_base_v1_turn_lineage_still_validates() -> None:
    state = DynamicStrategyState.model_validate_json(_V1_STATE_WITH_TURN_LINEAGE)

    assert state.planner.turn is not None
    assert state.planner.turn.lineage == (
        InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="older"),
            generation=0,
        ),
    )
