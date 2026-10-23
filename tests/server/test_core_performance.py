"""Core facts survive the card/page boundary for field-shaped histories."""

import base64
import gzip
import json

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.performance_cases import FIELD, ROOT, generated_run

from server.run_digest import digest_store
from vs_proto.api import Direction, IdeaOutcome, Outcome, outcome_presentation, validate
from vs_runlog.api import project_head


@pytest.mark.regression
@example(shape=2, seed=0, direction=Direction.MAX)
@example(shape=4, seed=2, direction=Direction.MIN)
@given(
    shape=st.integers(0, len(FIELD["shapes"]) - 1),
    seed=st.integers(0, 50),
    direction=st.sampled_from([Direction.MIN, Direction.MAX]),
)
def test_failed_noise_and_worse_ideas_never_advance_the_core_best(
    shape: int, seed: int, direction: Direction
) -> None:
    state, baseline = generated_run(shape, seed, direction)
    head = project_head(state)
    assert head is not None
    validate(head)
    facts = head.performance[0]
    assert head.objective is not None
    assert facts.input_value == baseline
    kept = [p for p in facts.points if p.new_best]
    assert all(p.outcome == IdeaOutcome.KEPT for p in kept)
    assert len({p.idea for p in kept}) == len(kept)
    for point in kept:
        assert point.idea is not None
        assert point.point is not None
        assert point.point.span is not None
        index = int(point.point.span.rsplit("-", 1)[1])
        assert index >= FIELD["shapes"][shape]["failed"]
        assert not (seed % 3 == 1 and index == 0)
        assert index % 4 == 0
    assert all(not p.new_best for p in facts.points if not p.has_field("idea"))
    assert all(p.point is not None for p in kept)
    standing = baseline
    for point in kept:
        assert point.point is not None
        value = point.point.value
        assert value < standing if direction == Direction.MIN else value > standing
        standing = value
    assert len(facts.ideas) >= len({p.idea for p in facts.points if p.has_field("idea")})
    assert head.objective.best_value == facts.best_value == standing
    card = digest_store(head, state)
    assert card.dashboard is not None
    assert card.dashboard.best is not None
    assert card.dashboard.best.value == standing
    assert len(card.dashboard.points) == len(facts.ideas)
    assert head.lifecycle is not None
    assert head.lifecycle.state is not None
    assert head.lifecycle.state.field == "ended"
    assert head.lifecycle.state.value.presentation == outcome_presentation(
        head.lifecycle.state.value.outcome
    )


def test_unknown_outcomes_are_named_validation_errors() -> None:
    with pytest.raises(ValueError, match=r"ended.outcome"):
        outcome_presentation(Outcome.UNSPECIFIED)


def corpus() -> list[dict[str, object]]:
    """Actual core and card outputs are replayed by the client, not hand-written expected bests."""
    cases = []
    for shape in range(len(FIELD["shapes"])):
        for seed in range(5):
            state, _ = generated_run(
                shape, seed, Direction.MIN if seed % 2 or seed == 4 else Direction.MAX
            )
            head = project_head(state)
            assert head is not None
            card = digest_store(head, state)
            assert head.lifecycle is not None
            assert head.lifecycle.state is not None
            assert head.lifecycle.state.field == "ended"
            assert head.lifecycle.state.value.presentation is not None
            cases.append(
                {
                    "name": f"shape-{shape}-seed-{seed}",
                    "presentation": json.loads(head.lifecycle.state.value.presentation.to_json()),
                    "head": base64.b64encode(head.to_binary()).decode(),
                    "skeleton": base64.b64encode(state.to_binary()).decode(),
                    "card": json.loads(card.model_dump_json()),
                }
            )
    return cases


def test_cross_language_corpus_is_the_core_projection() -> None:
    assert json.loads(gzip.decompress((ROOT / "cases.json.gz").read_bytes())) == corpus()


if __name__ == "__main__":
    (ROOT / "cases.json.gz").write_bytes(
        gzip.compress((json.dumps(corpus(), separators=(",", ":")) + "\n").encode(), mtime=0)
    )


def test_an_unknown_direction_is_a_typed_refusal_not_a_maximum() -> None:
    state, _ = generated_run(2, 0, Direction.MAX)
    assert state.epochs[0].epoch is not None
    assert state.epochs[0].epoch.run is not None
    state.epochs[0].epoch.run.objectives[0].direction = Direction.UNSPECIFIED
    head = project_head(state)
    assert head is not None
    assert head.performance[0].has_field("refusal")
    assert not any(point.new_best for point in head.performance[0].points)
    assert head.objective is not None
    assert not head.objective.has_field("best_value")
