"""The scripted agents of an agent-less dynamic run, through the client a run builds.

Each case drives the public surface a run uses: the client for the ``scripted`` backend, asked in
the way the dynamic search asks (the role's agent kind, a prompt, a workspace, the role's reply
type). What is checked is what the search depends on: replies the real reply models accept,
choices that depend on the seed and nothing else, source edits that land in the workspace, and the
whole range of ways a turn can fail.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from launch.agents import default_agent_client_factory
from vibesys.orchestration.dynamic.models import (
    ImplementerResult,
    PortfolioPlan,
    ReviewResult,
    planner_response_type,
)
from vibesys.orchestration.dynamic.scripted.api import (
    IMPLEMENTER_KIND,
    JUDGE_KIND,
    PLANNER_KIND,
    QUEUE_RECIPES,
    EditKind,
)
from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentBackend,
    AgentClientProtocol,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSpec,
    SessionScope,
)
from vs_core.api import Value
from vs_sim.api.testing import SimThreads

REPO_ROOT = Path(__file__).resolve().parents[4]
QUEUE_EXAMPLE = REPO_ROOT / "examples/data-structures/repositories/queue-rs"
SEEDS = st.integers(min_value=0, max_value=10_000)
ORDINALS = st.integers(min_value=1, max_value=400)


def _client(seed: int) -> AgentClientProtocol:
    factory = default_agent_client_factory(threads=SimThreads())
    return factory(
        spec=AgentSpec(backend=AgentBackend.SCRIPTED, scripted_seed=seed),
        events=NULL_AGENT_EVENT_SINK,
    )


def _copy_queue(root: Path) -> Path:
    shutil.copytree(
        QUEUE_EXAMPLE, root, ignore=shutil.ignore_patterns("target", ".git", ".vibesys")
    )
    return root


@pytest.fixture
def queue_checkout(tmp_path: Path) -> Path:
    """A copy of the queue example's sources, which the scripted agents recognize."""
    return _copy_queue(tmp_path / "queue")


@pytest.fixture(scope="module")
def queue_input(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The same sources for turns that only read them, shared by a property's examples."""
    return _copy_queue(tmp_path_factory.mktemp("input") / "queue")


def _planning_prompt(slots: int, offered: tuple[tuple[str, str], ...] = (), seen: str = "") -> str:
    candidates = "".join(
        f"- option `o{index}`, hypothesis `{hypothesis}`, revision `{revision}`, submission 1\n"
        for index, (hypothesis, revision) in enumerate(offered)
    )
    return (
        f"Schedule at most {slots} new workstreams for free slots. 0 other workstreams are "
        f"still running.\n{seen}\nBuildable candidates:\n{candidates}"
    )


def _plan(
    client: AgentClientProtocol,
    workspace: Path,
    prompt: str,
    revisions: tuple[str, ...] = (),
) -> PortfolioPlan:
    return client.invoke(
        kind=PLANNER_KIND,
        workspace=workspace,
        system_prompt="plan",
        user_prompt=prompt,
        response_cls=planner_response_type(revisions, None),
        round_label="plan",
    )


class _Envelope(Value):
    """The reply envelope of a role that may wait for evaluations, around a role's own reply."""

    reply: ImplementerResult | ReviewResult


def _member_key(kind: str, hypothesis: str) -> AgentSessionKey:
    return AgentSessionKey(SessionScope.MEMBER, f"{kind}:{hypothesis}")


def _implement(
    client: AgentClientProtocol, workspace: Path, hypothesis: str, *, correction: bool = False
) -> ImplementerResult:
    prompt = (
        "Your last reply was refused; reply again."
        if correction
        else f"Own hypothesis `{hypothesis}` in this isolated candidate workspace.\n"
    )
    return client.invoke(
        kind=IMPLEMENTER_KIND,
        workspace=workspace,
        system_prompt="implement",
        user_prompt=prompt,
        response_cls=ImplementerResult,
        round_label="implement",
        session_key=_member_key(IMPLEMENTER_KIND, hypothesis),
    )


def _judge(client: AgentClientProtocol, workspace: Path, hypothesis: str) -> ReviewResult:
    return client.invoke(
        kind=JUDGE_KIND,
        workspace=workspace,
        system_prompt="judge",
        user_prompt=f"Review hypothesis `{hypothesis}` without editing its candidate.\n",
        response_cls=ReviewResult,
        round_label="judge",
    )


def _ideas(plan: PortfolioPlan) -> list[str]:
    return [json.loads(item.model_dump_json())["hypothesis_id"] for item in plan.workstreams]


@given(seed=SEEDS, slots=st.integers(min_value=1, max_value=8))
@settings(max_examples=40, deadline=None)
def test_a_plan_fills_the_free_slots_up_to_its_cap_with_distinct_new_ideas(
    seed: int, slots: int, queue_input: Path
) -> None:
    plan = _plan(_client(seed), queue_input, _planning_prompt(slots))

    ideas = _ideas(plan)
    assert len(ideas) == min(slots, 3)
    assert len(set(ideas)) == len(ideas)
    assert {note.work_id for note in plan.handoff.expectations} == set(ideas)


@given(seed=SEEDS)
@settings(max_examples=40, deadline=None)
def test_a_later_plan_never_reuses_an_idea_the_run_already_has(
    seed: int, queue_input: Path
) -> None:
    taken = _ideas(_plan(_client(seed), queue_input, _planning_prompt(3)))
    seen = "History: " + ", ".join(f"`{name}`" for name in taken)

    later = _ideas(_plan(_client(seed), queue_input, _planning_prompt(3, seen=seen)))

    assert not set(later) & set(taken)


@given(seed=SEEDS)
@settings(max_examples=40, deadline=None)
def test_the_same_seed_and_prompt_give_the_same_plan(seed: int, queue_input: Path) -> None:
    prompt = _planning_prompt(3)

    first = _plan(_client(seed), queue_input, prompt)
    second = _plan(_client(seed), queue_input, prompt)

    assert first.model_dump_json() == second.model_dump_json()


def test_different_seeds_plan_different_ideas(queue_input: Path) -> None:
    plans = {
        tuple(_ideas(_plan(_client(seed), queue_input, _planning_prompt(3)))) for seed in range(30)
    }

    assert len(plans) > 5


@given(seed=SEEDS, picks=st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=6))
@settings(max_examples=40, deadline=None)
def test_an_idea_that_builds_on_a_candidate_names_one_the_prompt_offered(
    seed: int, picks: list[int], queue_input: Path
) -> None:
    offered = tuple((f"older-{index + 1}", f"rev{index:04d}" * 5) for index in range(6))
    chosen = tuple(offered[index] for index in dict.fromkeys(picks))
    plan = _plan(
        _client(seed),
        queue_input,
        _planning_prompt(3, offered=chosen),
        revisions=tuple(revision for _, revision in chosen),
    )

    for item in (json.loads(entry.model_dump_json()) for entry in plan.workstreams):
        parent = item.get("parent_hypothesis_id")
        if parent is not None:
            assert (parent, item["parent_revision"]) in chosen


def test_some_ideas_build_on_a_candidate_and_some_start_from_main(queue_input: Path) -> None:
    offered = (("older-1", "a" * 40), ("older-2", "b" * 40))
    parents = [
        json.loads(entry.model_dump_json()).get("parent_hypothesis_id")
        for seed in range(30)
        for entry in _plan(
            _client(seed),
            queue_input,
            _planning_prompt(3, offered=offered),
            revisions=tuple(revision for _, revision in offered),
        ).workstreams
    ]

    assert None in parents
    assert {"older-1", "older-2"} <= set(parents)


def test_a_project_the_agents_do_not_know_is_planned_as_exploration_and_blocks_its_implementer(
    tmp_path: Path,
) -> None:
    client = _client(0)

    ideas = _ideas(_plan(client, tmp_path, _planning_prompt(1)))
    reply = _implement(client, tmp_path, ideas[0])

    assert ideas[0].startswith("explore-")
    assert reply.outcome.value == "blocked"


def test_an_implementer_edit_lands_in_the_workspace_and_is_nominated(queue_checkout: Path) -> None:
    client = _client(0)
    source = queue_checkout / "src/lib.rs"
    before = source.read_text(encoding="utf-8")

    outcomes = {}
    for ordinal in range(1, 60):
        for recipe in QUEUE_RECIPES:
            source.write_text(before, encoding="utf-8")
            identifier = f"{recipe.slug}-{ordinal}"
            try:
                reply = _implement(client, queue_checkout, identifier)
            except (AgentOutputSchemaError, ConnectionError):
                continue
            if source.read_text(encoding="utf-8") != before:
                assert reply.outcome.value == "nominated"
                assert f"[scripted] {recipe.slug}:" in source.read_text(encoding="utf-8")
                outcomes[recipe.slug] = reply

    assert set(outcomes) == {recipe.slug for recipe in QUEUE_RECIPES}


def test_implementer_turns_fail_in_every_way_a_real_one_can(queue_checkout: Path) -> None:
    client = _client(0)
    source = queue_checkout / "src/lib.rs"
    before = source.read_text(encoding="utf-8")
    seen: set[str] = set()
    for ordinal in range(1, 400):
        source.write_text(before, encoding="utf-8")
        try:
            reply = _implement(client, queue_checkout, f"inline-hot-path-{ordinal}")
        except AgentOutputSchemaError:
            seen.add("refused-reply")
        except ConnectionError:
            seen.add("lost-turn")
        else:
            edited = source.read_text(encoding="utf-8") != before
            seen.add(f"{reply.outcome.value}{'+edit' if edited else ''}")

    assert seen == {
        "nominated+edit",
        "nominated",
        "implementation_failed",
        "blocked",
        "refused-reply",
        "lost-turn",
    }


def test_a_correction_turn_finds_its_hypothesis_by_its_conversation(queue_checkout: Path) -> None:
    client = _client(0)

    # The same hypothesis fails the same way whether it is named by the prompt or by the session.
    for ordinal in range(1, 400):
        identifier = f"inline-hot-path-{ordinal}"
        try:
            named = _implement(client, queue_checkout, identifier)
        except (AgentOutputSchemaError, ConnectionError) as error:
            with pytest.raises(type(error)):
                _implement(client, queue_checkout, identifier, correction=True)
            return
        assert named.outcome.value in {
            "nominated",
            "implementation_failed",
            "blocked",
            "inconclusive",
        }
    pytest.fail("no hypothesis failed its turn in 400 tries")


def test_a_judge_both_passes_and_rejects(queue_checkout: Path) -> None:
    client = _client(0)

    verdicts = {_judge(client, queue_checkout, f"single-copy-{n}").passed for n in range(1, 80)}

    assert verdicts == {True, False}


def test_a_regressing_edit_is_rejected_more_often_than_an_improving_one(
    queue_checkout: Path,
) -> None:
    client = _client(0)
    recipes = {recipe.kind: recipe.slug for recipe in QUEUE_RECIPES}

    def rejected(slug: str) -> int:
        return sum(not _judge(client, queue_checkout, f"{slug}-{n}").passed for n in range(1, 300))

    assert rejected(recipes[EditKind.REGRESSES]) > 2 * rejected(recipes[EditKind.IMPROVES])


def test_every_recipe_applies_once_and_only_once_to_the_queue_source() -> None:
    source = (QUEUE_EXAMPLE / "src/lib.rs").read_text(encoding="utf-8")

    for recipe in QUEUE_RECIPES:
        edited = recipe.apply(source)
        assert edited is not None, recipe.slug
        assert edited != source
        assert recipe.apply(edited) is None, recipe.slug


def test_the_kinds_of_edit_cover_every_way_a_candidate_can_measure() -> None:
    assert {recipe.kind for recipe in QUEUE_RECIPES} == set(EditKind)


def test_the_scripted_backend_reports_itself_and_answers_conversation() -> None:
    client = _client(0)

    assert client.backend_name == "scripted"
    answer = client.invoke_text(
        kind="chat",
        workspace=Path(),
        system_prompt="chat",
        user_prompt="what happened?",
        round_label="chat",
    )
    assert "scripted agents" in answer


def test_a_role_without_a_script_fails_its_turn_as_a_refused_reply(tmp_path: Path) -> None:
    with pytest.raises(AgentOutputSchemaError):
        _client(0).invoke(
            kind="dynamic-merger",
            workspace=tmp_path,
            system_prompt="merge",
            user_prompt="merge",
            response_cls=ReviewResult,
            round_label="merge",
        )
