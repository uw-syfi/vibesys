"""The run-level prompt variables and the planner example every dynamic template can read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import ImplementPortfolioPlan

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def run_prompt_variables(  # noqa: PLR0913  # LW-930099 [PLR0913]; the variables are independent facts of the run that no one record holds.
    *,
    objective: str,
    environment_notes: str,
    agent_evaluation: bool,
    agent_git_writable: bool,
    domain_sections: Mapping[str, str],
    comparisons: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """The run-level variables every dynamic template reads, besides its own context's fields.

    The plan carries this mapping and the renderer adds it to each prompt, so a template
    that reads a new run-level variable gets it from here for production and for tests alike.
    """
    return {
        "objective": objective,
        "environment_notes": environment_notes,
        "agent_evaluation": agent_evaluation,
        "agent_git_writable": agent_git_writable,
        "domain_sections": domain_sections,
        "comparisons": comparisons,
        "plan_example": planner_reply_example(),
    }


def planner_reply_example() -> str:
    """A valid planner reply with placeholder values, rendered from the reply model itself.

    The planner prompt shows it so the model copies the shape of every field instead of
    guessing it. It is built from the model, so a renamed or newly required field breaks
    this function rather than leaving the prompt describing a reply the schema rejects.
    """
    ordinals = ("first", "second")
    plan = ImplementPortfolioPlan.model_validate(
        {
            "reasoning": "Why these workstreams test different mechanisms.",
            "workstreams": [
                {
                    "hypothesis_id": f"{ordinal}-idea",
                    "title": f"Name of the {ordinal} goal",
                    "hypothesis": f"The claim the {ordinal} workstream tests.",
                    "task": f"The concrete changes the {ordinal} implementer makes.",
                    "pass_criteria": f"Observable evidence that the {ordinal} goal is met.",
                }
                for ordinal in ordinals
            ],
            "hypothesis_updates": [],
            "handoff": {
                "expectations": [
                    {
                        "work_id": f"{ordinal}-idea",
                        "expected": f"What the {ordinal} workstream should show.",
                        "if_it_succeeds": f"What to plan next if the {ordinal} claim holds.",
                        "if_it_fails": f"What to plan next if the {ordinal} claim fails.",
                    }
                    for ordinal in ordinals
                ],
                "doubts": ["What is still unclear and how to check it."],
            },
        }
    )
    return plan.model_dump_json(indent=2)
