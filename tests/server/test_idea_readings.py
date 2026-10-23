"""A later unmeasured check cannot erase an idea's core measurement identity."""

from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.performance_cases import FIELD, generated_run

from vs_proto.api import Direction, Span, SpanHistory, SpanVersion
from vs_runlog.api import project_head


@example(shape=2, seed=0, direction=Direction.MAX, checks=1)
@given(
    shape=st.integers(0, len(FIELD["shapes"]) - 1),
    seed=st.integers(0, 50),
    direction=st.sampled_from([Direction.MIN, Direction.MAX]),
    checks=st.integers(1, 3),
)
def test_measured_ideas_keep_their_reading_after_unmeasured_checks(
    shape: int, seed: int, direction: Direction, checks: int
) -> None:
    state, baseline = generated_run(shape, seed, direction)
    before = project_head(state)
    assert before is not None
    for key, history in list(state.spans.items()):
        original = history.versions[-1].span
        if original is None or original.label is None or original.label.kind != "gate":
            continue
        for index in range(checks):
            span = Span.from_binary(original.to_binary())
            span.id = f"{key}-check-{index}"
            span.started_at += 1000 + index
            state.through_seq += 1
            state.spans[span.id] = SpanHistory(
                versions=[SpanVersion(seq=state.through_seq, span=span)]
            )
    after = project_head(state)
    assert after is not None
    assert after.performance == before.performance
    for series in after.performance:
        assert series.input_value == baseline
        points = {point.measurement_seq: point for point in series.points}
        for idea in series.ideas:
            if idea.has_field("measurement_seq"):
                assert points[idea.measurement_seq].idea == idea.idea
                assert points[idea.measurement_seq].point is not None
