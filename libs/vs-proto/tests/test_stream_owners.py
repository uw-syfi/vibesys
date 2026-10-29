"""Native stream owners are validated semantic values, independent of display text."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p


@st.composite
def owners(draw: st.DrawFn) -> p.StreamOwner:
    kind = draw(st.sampled_from(("startup", "gate", "tool_build", "narration")))
    if kind == "startup":
        return p.StreamOwner(
            kind=p.Oneof(
                "startup",
                draw(
                    st.sampled_from(
                        [
                            p.StartupStage.PROBING_WORKSPACE,
                            p.StartupStage.BUILDING_IMAGE,
                            p.StartupStage.BUILDING_TOOLS,
                            p.StartupStage.STARTING_CONTAINER,
                        ]
                    )
                ),
            )
        )
    if kind == "gate":
        return p.StreamOwner(
            kind=p.Oneof(
                "gate",
                p.GateStreamOwner(
                    execution="evaluation-a",
                    gate=draw(
                        st.sampled_from(
                            [
                                p.LogGate.BUILD,
                                p.LogGate.ACCURACY,
                                p.LogGate.BENCHMARK,
                                p.LogGate.VALIDATION,
                            ]
                        )
                    ),
                    attempt=draw(st.integers(min_value=0, max_value=2**32 - 1)),
                ),
            )
        )
    if kind == "tool_build":
        return p.StreamOwner(
            kind=p.Oneof(
                "tool_build",
                draw(
                    st.text(
                        alphabet=st.characters(exclude_categories=("Cs",)),
                        min_size=1,
                        max_size=32,
                    )
                ),
            )
        )
    return p.StreamOwner(kind=p.Oneof("narration", value=True))


@given(owner=owners(), closed=st.booleans(), length=st.integers(min_value=0, max_value=2**32))
def test_semantic_log_owner_roundtrips_for_open_and_closed_streams(
    owner: p.StreamOwner,
    *,
    closed: bool,
    length: int,
) -> None:
    stream = p.StreamRef(
        id="log-a",
        kind=p.StreamKind.LOG,
        title="unrelated display label",
        owner=owner,
        state=p.StreamState.ENDED if closed else p.StreamState.OPENED,
        length=length if closed else None,
    )
    p.validate(stream)
    assert p.StreamRef.from_binary(stream.to_binary()) == stream


@pytest.mark.parametrize(
    "owner",
    [
        p.StreamOwner(),
        p.StreamOwner(kind=p.Oneof("startup", p.StartupStage.UNSPECIFIED)),
        p.StreamOwner(kind=p.Oneof("gate", p.GateStreamOwner(execution="a"))),
        p.StreamOwner(kind=p.Oneof("gate", p.GateStreamOwner(gate=p.LogGate.BUILD))),
        p.StreamOwner(kind=p.Oneof("tool_build", "")),
        p.StreamOwner(kind=p.Oneof("narration", value=False)),
    ],
)
def test_invalid_log_ownership_is_rejected(owner: p.StreamOwner) -> None:
    with pytest.raises(p.ValidationError):
        p.validate(owner)


def test_log_owner_cannot_be_used_as_transcript_ownership() -> None:
    with pytest.raises(p.ValidationError) as failure:
        p.validate(
            p.StreamRef(
                id="transcript-a",
                kind=p.StreamKind.TRANSCRIPT,
                span="turn-a",
                state=p.StreamState.OPENED,
                owner=p.StreamOwner(kind=p.Oneof("narration", value=True)),
            )
        )
    assert any(
        violation.rule_id == "stream.owner" for violation in failure.value.to_proto().violations
    )
    historical = p.StreamRef(id="log-a", kind=p.StreamKind.LOG, state=p.StreamState.OPENED)
    p.validate(historical)
    assert p.StreamRef.from_binary(historical.to_binary()).owner is None
