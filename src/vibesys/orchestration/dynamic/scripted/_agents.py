"""The scripted planner, implementer and judge of a dynamic run, as pure decisions.

Each function answers one agent turn the way a competent agent would, from the facts of the turn
alone: the planner reads its free slots and the candidates it may build on out of its prompt text; the
implementer and the judge are told their hypothesis and the source of the candidate. Nothing is
remembered between turns, so a resumed run, and turns that run in parallel, behave the same as an
uninterrupted serial one. Reading and writing the workspace, and raising the failures, belong to the
shell that calls these (`launch.scripted_agents`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.scripted._choice import index, unit
from vibesys.orchestration.dynamic.scripted._recipes import EditKind, Recipe, applied_slugs
from vibesys.orchestration.dynamic.scripted._scenario import (
    Fault,
    Scenario,
    hypothesis_id,
    idea,
    ordinal_of,
    scenario,
)

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.scripted._workload import Workload

MAX_PLANNED = 3
"""The most workstreams one planning turn starts, however many slots are free."""
_PARENT_SHARE = 0.5
"""The share of new ideas that build on an earlier candidate instead of on main."""

_SLOTS = re.compile(r"Schedule at most (\d+) new workstreams")
_OFFERED = re.compile(r"hypothesis `(?P<hypothesis>[^`\n]+)`, revision `(?P<revision>[^`\n]+)`")
_IDEA = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)*-\d+)`")
_MEMBER = re.compile(r"^(?:Own|Review) hypothesis `(?P<id>[^`\n]+)`")

EXPLORE = Recipe(
    slug="explore",
    kind=EditKind.NEUTRAL,
    title="Look for a change worth making",
    claim="The code has a change that raises throughput; the first step is to find it.",
    task="Read the code, and make the smallest change that tests where time goes.",
    evidence_wanted="The check passes and throughput does not fall.",
    summary="",
    edit=lambda _source: None,
)
"""The idea a planner pitches for a project it has no recipes for; its implementer reports blocked."""

CHAT_REPLY = (
    "This run uses scripted agents: no model is attached, so there is nothing here to answer "
    "questions. The run's records are the real ones."
)


def named_in(prompt: str) -> str | None:
    """The hypothesis a turn's prompt names, or ``None`` for a turn that does not (a correction)."""
    found = _MEMBER.match(prompt)
    return found.group("id") if found else None


@dataclass(frozen=True, slots=True)
class Turn:
    """What a scripted turn answers: its reply, what it says along the way, and its edit."""

    reply: dict[str, object] | None
    """The reply, or ``None`` for a turn that fails (see ``failure``)."""
    narration: tuple[str, ...] = ()
    edited_source: str | None = None
    """The workload source after the turn's edit, when it edited."""
    failure: Fault | None = None
    """How the turn fails instead of replying: a reply the schema refuses, or a lost turn."""


def _recipes(workload: Workload | None) -> tuple[Recipe, ...]:
    return workload.recipes if workload is not None else ()


def plan(seed: int, workload: Workload | None, prompt: str) -> Turn:
    """Plan the free slots: new ideas, some built on an earlier candidate."""
    slots = _SLOTS.search(prompt)
    free = int(slots.group(1)) if slots else 1
    offered = [(found["hypothesis"], found["revision"]) for found in _OFFERED.finditer(prompt)]
    seen = (ordinal_of(found) or 0 for found in _IDEA.findall(prompt))
    first = max(seen, default=0) + 1
    workstreams = [
        _workstream(seed, _recipes(workload), ordinal, offered)
        for ordinal in range(first, first + min(free, MAX_PLANNED))
    ]
    names = ", ".join(str(item["hypothesis_id"]) for item in workstreams)
    reply: dict[str, object] = {
        "reasoning": (
            "Each idea changes a different part of the hot path, so their results say which "
            "part of the queue limits throughput; ideas that build on an earlier candidate "
            "test whether a gain adds up."
        ),
        "workstreams": workstreams,
        "hypothesis_updates": [],
        "handoff": {
            "expectations": [
                {
                    "work_id": item["hypothesis_id"],
                    "expected": f"{item['title']} changes throughput measurably.",
                    "if_it_succeeds": "Build the next idea on top of it.",
                    "if_it_fails": "Drop the idea and try a different part of the hot path.",
                }
                for item in workstreams
            ],
            "doubts": [],
        },
    }
    narration = (
        "Reading the candidates measured so far and what each one cost.",
        f"Scheduling {len(workstreams)} independent ideas: {names}.",
    )
    return Turn(reply=reply, narration=narration)


def _workstream(
    seed: int, recipes: tuple[Recipe, ...], ordinal: int, offered: list[tuple[str, str]]
) -> dict[str, object]:
    recipe = idea(seed, recipes, ordinal) if recipes else EXPLORE
    identifier = hypothesis_id(recipe, ordinal)
    entry: dict[str, object] = {
        "kind": "implement",
        "hypothesis_id": identifier,
        "title": recipe.title,
        "hypothesis": recipe.claim,
        "task": recipe.task,
        "pass_criteria": recipe.evidence_wanted,
        "continue_hypothesis": False,
    }
    if offered and unit(seed, "parent", identifier) < _PARENT_SHARE:
        parent = offered[index(seed, len(offered), "base", identifier)]
        entry["parent_hypothesis_id"], entry["parent_revision"] = parent
    return entry


def implement(seed: int, workload: Workload | None, identifier: str, source: str | None) -> Turn:
    """Edit the candidate's ``source`` as the hypothesis says, or fail as its scenario says."""
    outcome = scenario(seed, _recipes(workload), identifier)
    opening = f"Starting on `{identifier}`: reading the parent revision."
    failed = _fault(outcome, opening)
    if failed is not None:
        return failed
    if workload is None or outcome.recipe is None or source is None:
        return Turn(
            reply=_reply(
                "blocked",
                "No scripted change is known for this project, so nothing was edited.",
                next_step="Plan work for a project the scripted agents know (the queue example).",
            ),
            narration=(opening,),
        )
    recipe = outcome.recipe
    edited = recipe.apply(source)
    if edited is None:
        return Turn(
            reply=_reply(
                "inconclusive",
                f"The parent revision already contains `{recipe.slug}`, or the code it changes "
                "has moved; no edit was made.",
            ),
            narration=(opening, f"`{recipe.slug}` is already in the parent, or its code moved."),
        )
    return Turn(
        reply=_reply(
            "nominated",
            recipe.summary,
            evidence=[{"location": workload.source, "purpose": f"the edit for {recipe.slug}"}],
        ),
        narration=(
            opening,
            f"Applying the change: {recipe.title}.",
            "Building and running the check before nominating the candidate.",
        ),
        edited_source=edited,
    )


def _fault(outcome: Scenario, opening: str) -> Turn | None:
    match outcome.fault:
        case None:
            return None
        case Fault.GIVES_UP:
            reply = _reply(
                "implementation_failed",
                "The idea needs a restructuring of the queue that one turn cannot finish; "
                "nothing was changed.",
            )
            return Turn(
                reply=reply,
                narration=(opening, "The change does not fit the code as it stands; giving up."),
            )
        case Fault.NO_EDIT:
            reply = _reply("nominated", "The code already does what the idea asks.")
            return Turn(
                reply=reply, narration=(opening, "Checked the code and found nothing to change.")
            )
        case Fault.BLOCKED:
            reply = _reply(
                "blocked",
                "The change needs a measurement the workspace cannot take.",
                next_step="Measure it outside the run, then plan the change again.",
            )
            return Turn(
                reply=reply, narration=(opening, "A tool this change needs is not available here.")
            )
        case Fault.BAD_REPLY | Fault.LOST_TURN:
            return Turn(reply=None, narration=(opening,), failure=outcome.fault)


def judge(seed: int, workload: Workload | None, identifier: str, source: str | None) -> Turn:
    """Read the candidate's ``source``, and pass it or reject it as its scenario says."""
    outcome = scenario(seed, _recipes(workload), identifier)
    carried = applied_slugs(source) if source is not None else ()
    narration = (f"Reading the candidate for `{identifier}` and what the framework measured.",)
    if outcome.rejected:
        reply: dict[str, object] = {
            "passed": False,
            "analysis": (
                "The evaluations do not support the claim: the change either costs "
                "throughput or gains less than the noise between repetitions."
            ),
            "feedback": "Try the idea at a different point of the hot path, or drop it.",
        }
    else:
        edits = ", ".join(f"`{slug}`" for slug in carried) or "no scripted edit"
        reply = {
            "passed": True,
            "analysis": (
                f"The candidate carries {edits}; it builds, passes the trusted check and "
                "the measurement is consistent with the claim."
            ),
            "feedback": "",
        }
    return Turn(reply=reply, narration=narration)


def _reply(
    outcome: str,
    summary: str,
    *,
    evidence: list[dict[str, str]] | None = None,
    next_step: str = "",
) -> dict[str, object]:
    return {
        "summary": summary,
        "outcome": outcome,
        "evidence": evidence or [],
        "next_step": next_step,
    }
