"""Scenarios over the fake core: corrections, review, selection, stop, and persistence."""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.dynamic.strategy._fake_core import (
    FakeCore,
    Script,
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._harness import envelope, round_trip

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    RenderRoleArtifacts,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ControlChanged,
    ControlId,
    ControlInput,
    Operation,
    ProposeWinner,
    RequestTurn,
    StartAttempt,
    Stop,
)


def _config(**overrides: object) -> DynamicConfig:
    return DynamicConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 1,
            "max_in_flight": 1,
            **overrides,
        }
    )


def _core(script: Script, **overrides: object) -> FakeCore:
    return FakeCore(strategy=DynamicStrategy(config=_config(**overrides)), script=script)


def _count(core: FakeCore, kind: type) -> int:
    return sum(isinstance(item, kind) for item in core.decisions)


def test_invalid_plan_gets_one_correction_turn() -> None:
    """Ports test_plan_recovery: a malformed plan is corrected once, then accepted."""
    script = Script(
        planner=deque(["not json", plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    core = _core(script)
    core.run()
    planner_turns = [
        item
        for item in core.decisions
        if isinstance(item, RequestTurn) and item.turn.session.role_id.root.endswith("orchestrator")
    ]
    assert len(planner_turns) == 2
    assert _count(core, StartAttempt) == 1
    assert isinstance(core.decisions[-1], Stop)


def test_every_prompt_goes_through_a_render_operation() -> None:
    """Prompts are rendered by a declared operation before each turn is requested."""
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    core = _core(script)
    core.run()
    renders = [
        item
        for item in core.decisions
        if isinstance(item, Operation) and isinstance(item.request, RenderRoleArtifacts)
    ]
    assert len(renders) == _count(core, RequestTurn)


def test_best_of_two_measured_candidates_is_proposed() -> None:
    """The strongest eligible candidate wins over a weaker one."""
    values = {"rev:attempt:h1": 70.0, "rev:attempt:h2": 90.0}
    script = Script(
        planner=deque([plan_reply(implement("h1"), implement("h2"))]),
        implementer=deque([implemented(), implemented()]),
        judge=deque([reviewed(), reviewed()]),
        benchmark=lambda revision: next(v for k, v in values.items() if revision.startswith(k)),
    )
    core = _core(script, max_in_flight=2)
    core.run()
    proposal = next(item for item in core.decisions if isinstance(item, ProposeWinner))
    assert "h2" in repr(proposal.selection)


def test_no_improvement_selects_the_trusted_baseline() -> None:
    """A candidate that does not beat the baseline is not adopted."""
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
        benchmark=lambda _revision: 10.0,
    )
    core = _core(script)
    core.run()
    proposal = next(item for item in core.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"


def test_stop_control_drains_and_stops() -> None:
    """A stop control ends the run without proposing a winner for unfinished work."""
    script = Script(planner=deque([plan_reply(implement("h1"))]))
    core = _core(script)
    core.feed(
        ControlChanged(control=ControlInput(control_id=ControlId(root="stop"), action="stop"))
    )
    core.run()
    assert isinstance(core.decisions[-1], Stop)
    assert _count(core, StartAttempt) == 0


def test_state_round_trips_through_the_envelope_at_every_step() -> None:
    """The persisted state decodes to an equal value after every decide and event."""
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    core = _core(script)
    codec, saved = envelope()
    for _ in range(200):
        decided = core.step()
        saved = saved.model_copy(update={"strategy": core.strategy.state})
        assert round_trip(codec, saved).strategy == core.strategy.state
        if not decided:
            break
    assert isinstance(core.decisions[-1], Stop)


def test_operations_are_valid_for_the_registry() -> None:
    """Every emitted operation passes core's registry validation."""
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    core = _core(script)
    core.run()
    for item in core.decisions:
        if isinstance(item, Operation):
            core.codec.validate_decision(item)


def test_duplicate_events_do_not_change_the_outcome() -> None:
    """Delivering every strategy event twice yields the same decision sequence."""

    def run(*, duplicate: bool) -> list[str]:
        script = Script(
            planner=deque([plan_reply(implement("h1"))]),
            implementer=deque([implemented()]),
            judge=deque([reviewed()]),
        )
        core = _core(script)
        if duplicate:
            original = core.feed

            def twice(event: object) -> None:
                original(event)  # type: ignore[arg-type]
                original(event)  # type: ignore[arg-type]

            core.feed = twice  # type: ignore[method-assign]
        core.run()
        return [type(item).__name__ for item in core.decisions]

    assert run(duplicate=True) == run(duplicate=False)


def test_failed_review_makes_a_candidate_ineligible() -> None:
    """A judge verdict of not passed keeps the candidate from adoption."""
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed(passed=False)]),
    )
    core = _core(script)
    core.run()
    proposal = next(item for item in core.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "trusted_baseline"
