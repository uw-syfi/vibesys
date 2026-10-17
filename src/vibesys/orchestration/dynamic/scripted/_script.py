"""The scripted planner, implementer and judge of a dynamic run.

Each method answers one agent turn the way a competent agent would, from the turn alone: the
planner reads its free slots and the candidates it may build on out of its prompt; the implementer
and the judge find their hypothesis in theirs. What each does with its workspace is real: the
implementer edits the workload's source, the framework builds and measures it, and the judge reads
the edited file. Nothing is remembered between turns, so a resumed run, and turns that run in
parallel, behave the same as an uninterrupted serial one.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.scripted._choice import index, unit
from vibesys.orchestration.dynamic.scripted._recipes import EditKind, Recipe, applied_slugs
from vibesys.orchestration.dynamic.scripted._scenario import (
    Fault,
    hypothesis_id,
    idea,
    ordinal_of,
    scenario,
)
from vibesys.orchestration.dynamic.scripted._workload import WORKLOADS, Workload, recognized
from vs_agent.api import AgentOutputSchemaError, SessionScope

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.scripted._scenario import Scenario
    from vs_agent.api import AgentEventSink, ScriptedTurn

MAX_PLANNED = 3
"""The most workstreams one planning turn starts, however many slots are free."""
_PARENT_SHARE = 0.5
"""The share of new ideas that build on an earlier candidate instead of on main."""

_SLOTS = re.compile(r"Schedule at most (\d+) new workstreams")
_MEMBER = re.compile(r"^(?:Own|Review) hypothesis `(?P<id>[^`\n]+)`")
_OFFERED = re.compile(r"hypothesis `(?P<hypothesis>[^`\n]+)`, revision `(?P<revision>[^`\n]+)`")
_IDEA = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)*-\d+)`")

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


class DynamicScript:
    """The scripted agents of one run, all decided by ``seed`` and the turns they are given."""

    def __init__(
        self,
        *,
        seed: int,
        sink: AgentEventSink,
        workloads: tuple[Workload, ...] = WORKLOADS,
    ) -> None:
        """Script agents over ``workloads`` that narrate their turns to ``sink``."""
        self._seed = seed
        self._sink = sink
        self._workloads = workloads

    # -- planner -------------------------------------------------------------------------------

    def plan(self, turn: ScriptedTurn) -> dict[str, object]:
        """Plan the free slots: new ideas, some built on an earlier candidate."""
        slots = _SLOTS.search(turn.user_prompt)
        free = int(slots.group(1)) if slots else 1
        workload = recognized(turn.workspace, self._workloads)
        recipes = workload.recipes if workload is not None else ()
        offered = _OFFERED.findall(turn.user_prompt)
        first = (
            max((ordinal_of(found) or 0 for found in _IDEA.findall(turn.user_prompt)), default=0)
            + 1
        )
        self._say(turn, "Reading the candidates measured so far and what each one cost.")
        workstreams = [
            self._workstream(recipes, ordinal, offered)
            for ordinal in range(first, first + min(free, MAX_PLANNED))
        ]
        names = [str(item["hypothesis_id"]) for item in workstreams]
        self._say(turn, f"Scheduling {len(workstreams)} independent ideas: {', '.join(names)}.")
        return {
            "reasoning": (
                "Each idea changes a different part of the hot path, so their results say "
                "which part of the queue limits throughput; ideas that build on an earlier "
                "candidate test whether a gain adds up."
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

    def _workstream(
        self, recipes: tuple[Recipe, ...], ordinal: int, offered: list[tuple[str, str]]
    ) -> dict[str, object]:
        recipe = idea(self._seed, recipes, ordinal) if recipes else EXPLORE
        identifier = hypothesis_id(recipe, ordinal)
        plan: dict[str, object] = {
            "kind": "implement",
            "hypothesis_id": identifier,
            "title": recipe.title,
            "hypothesis": recipe.claim,
            "task": recipe.task,
            "pass_criteria": recipe.evidence_wanted,
            "continue_hypothesis": False,
        }
        parent = self._parent(identifier, offered)
        if parent is not None:
            plan["parent_hypothesis_id"], plan["parent_revision"] = parent
        return plan

    def _parent(self, identifier: str, offered: list[tuple[str, str]]) -> tuple[str, str] | None:
        if not offered or unit(self._seed, "parent", identifier) >= _PARENT_SHARE:
            return None
        return offered[index(self._seed, len(offered), "base", identifier)]

    # -- implementer ---------------------------------------------------------------------------

    def implement(self, turn: ScriptedTurn) -> dict[str, object]:
        """Edit the candidate as the hypothesis says, or fail the way its scenario says."""
        identifier = _member(turn)
        workload = recognized(turn.workspace, self._workloads)
        recipes = workload.recipes if workload is not None else ()
        outcome = scenario(self._seed, recipes, identifier)
        self._say(turn, f"Starting on `{identifier}`: reading the parent revision.")
        failed = self._fault(turn, outcome)
        if failed is not None:
            return failed
        if workload is None or outcome.recipe is None:
            return _reply(
                "blocked",
                "No scripted change is known for this project, so nothing was edited.",
                next_step="Plan work for a project the scripted agents know (the queue example).",
            )
        return self._edit(turn, workload, outcome.recipe)

    def _fault(self, turn: ScriptedTurn, outcome: Scenario) -> dict[str, object] | None:
        match outcome.fault:
            case None:
                return None
            case Fault.GIVES_UP:
                self._say(turn, "The change does not fit the code as it stands; giving up.")
                return _reply(
                    "implementation_failed",
                    "The idea needs a restructuring of the queue that one turn cannot finish; "
                    "nothing was changed.",
                )
            case Fault.NO_EDIT:
                self._say(turn, "Checked the code and found nothing to change.")
                return _reply("nominated", "The code already does what the idea asks.")
            case Fault.BLOCKED:
                self._say(turn, "A tool this change needs is not available here.")
                return _reply(
                    "blocked",
                    "The change needs a measurement the workspace cannot take.",
                    next_step="Measure it outside the run, then plan the change again.",
                )
            case Fault.BAD_REPLY:
                self._say(turn, "Writing the reply.")
                message = "the reply did not match the reply schema"
                raise AgentOutputSchemaError(message)
            case Fault.LOST_TURN:
                message = "the agent process ended before it replied"
                raise ConnectionError(message)

    def _edit(self, turn: ScriptedTurn, workload: Workload, recipe: Recipe) -> dict[str, object]:
        path = turn.workspace / workload.source
        edited = recipe.apply(path.read_text(encoding="utf-8"))
        if edited is None:
            self._say(turn, f"`{recipe.slug}` is already in the parent, or its code moved.")
            return _reply(
                "inconclusive",
                f"The parent revision already contains `{recipe.slug}`, or the code it changes "
                "has moved; no edit was made.",
            )
        self._say(turn, f"Applying the change: {recipe.title}.")
        path.write_text(edited, encoding="utf-8")
        self._say(turn, "Building and running the check before nominating the candidate.")
        return _reply(
            "nominated",
            recipe.summary,
            evidence=[{"location": workload.source, "purpose": f"the edit for {recipe.slug}"}],
        )

    # -- judge ---------------------------------------------------------------------------------

    def judge(self, turn: ScriptedTurn) -> dict[str, object]:
        """Read the candidate's source, and pass it or reject it as its scenario says."""
        identifier = _member(turn)
        workload = recognized(turn.workspace, self._workloads)
        recipes = workload.recipes if workload is not None else ()
        outcome = scenario(self._seed, recipes, identifier)
        carried = (
            applied_slugs((turn.workspace / workload.source).read_text(encoding="utf-8"))
            if workload is not None
            else ()
        )
        self._say(
            turn, f"Reading the candidate for `{identifier}` and what the framework measured."
        )
        if outcome.rejected:
            return {
                "passed": False,
                "analysis": (
                    "The evaluations do not support the claim: the change either costs "
                    "throughput or gains less than the noise between repetitions."
                ),
                "feedback": "Try the idea at a different point of the hot path, or drop it.",
            }
        return {
            "passed": True,
            "analysis": (
                f"The candidate carries {', '.join(f'`{slug}`' for slug in carried) or 'no scripted edit'}; "
                "it builds, passes the trusted check and the measurement is consistent with the claim."
            ),
            "feedback": "",
        }

    def _say(self, turn: ScriptedTurn, text: str) -> None:
        self._sink.agent_output(
            f"{text}\n",
            channel="assistant",
            agent_kind=turn.kind,
            round_label=turn.round_label,
            invocation_id=turn.invocation_id,
        )


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


def _member(turn: ScriptedTurn) -> str:
    """The hypothesis the turn is about: named in its prompt, else by its conversation."""
    named = _MEMBER.match(turn.user_prompt)
    if named is not None:
        return named.group("id")
    key = turn.session_key
    if key is not None and key.scope is SessionScope.MEMBER:
        role, separator, member = key.identifier.partition(":")
        if separator and role == turn.kind:
            return member
    if key is not None and key.scope is SessionScope.MEMBER_GENERATION:
        role, member, _generation = json.loads(key.identifier)
        if role == turn.kind:
            return str(member)
    message = f"the {turn.kind} turn names no hypothesis"
    raise AgentOutputSchemaError(message)


def wire(turn: ScriptedTurn, payload: dict[str, object]) -> dict[str, object]:
    """``payload`` as the turn's reply type reads it: inside the envelope of a role that may wait."""
    reply_type = turn.response_cls
    if reply_type is not None and "reply" in reply_type.model_fields:
        return {"reply": payload}
    return payload
