"""The normalizer is a deterministic fold: same events in, same sidecar events out."""

from __future__ import annotations

from agentshim import (
    AssistantText,
    ProviderUsage,
    Reasoning,
    SteerConsumed,
    SteerRejected,
    TextDelta,
    TextKind,
    TokenUsage,
    ToolCall,
    ToolResult,
    UsageReport,
)
from hypothesis import given
from hypothesis import strategies as st

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.normalize import SteerBook, TurnNormalizer, normalize, usage_proto

DONE = orch.TURN_STATUS_COMPLETED


def _usage(input_tokens: int, output_tokens: int, **more: int) -> ProviderUsage:
    return ProviderUsage(
        tokens=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, **more)
    )


def _bytes(events: list) -> list[bytes]:
    return [e.SerializeToString(deterministic=True) for e in events]


def test_text_and_reasoning_keep_the_provider_item_ids() -> None:
    out = normalize("t1", [Reasoning("hm", "r1"), AssistantText("hi", "a1")], DONE)
    items = [e.item for e in out if e.HasField("item")]
    assert [(i.id, i.WhichOneof("body")) for i in items] == [
        ("r1", "reasoning"),
        ("a1", "assistant"),
    ]
    assert all(i.status == orch.ITEM_STATUS_FINISHED and i.turn_id == "t1" for i in items)


def test_an_item_without_an_id_gets_a_stable_one() -> None:
    first = normalize("t1", [AssistantText("a"), AssistantText("b")], DONE)
    again = normalize("t1", [AssistantText("a"), AssistantText("b")], DONE)
    ids = [e.item.id for e in first if e.HasField("item")]
    assert len(set(ids)) == 2
    assert _bytes(first) == _bytes(again)


def test_a_tool_call_runs_then_finishes_under_one_id() -> None:
    out = normalize(
        "t1",
        [
            ToolCall("c1", "Bash", {"command": "ls"}),
            ToolResult("c1", "Bash", "a\n", "warn", 2, 0.1),
        ],
        DONE,
    )
    tools = [e.item for e in out if e.HasField("item")]
    assert [t.status for t in tools] == [orch.ITEM_STATUS_RUNNING, orch.ITEM_STATUS_FINISHED]
    assert {t.id for t in tools} == {"c1"}
    last = tools[-1].tool
    assert last.tool == "Bash"
    assert last.args_json == '{"command":"ls"}'
    assert last.output == "a\n\nwarn"
    assert last.exit_code == 2
    assert last.is_error


def test_a_tool_that_never_reports_is_failed_when_the_turn_does_not_complete() -> None:
    for status, expected in (
        (DONE, orch.ITEM_STATUS_FINISHED),
        (orch.TURN_STATUS_INTERRUPTED, orch.ITEM_STATUS_FAILED),
        (orch.TURN_STATUS_FAILED, orch.ITEM_STATUS_FAILED),
    ):
        out = normalize("t1", [ToolCall("c1", "Bash", None)], status)
        items = [e.item for e in out if e.HasField("item")]
        assert items[-1].status == expected
        assert out[-1].turn_ended.status == status


def test_a_repeated_event_adds_nothing() -> None:
    normalizer = TurnNormalizer("t1")
    event = AssistantText("same", "a1")
    assert len(normalizer.feed(event)) == 1
    assert normalizer.feed(event) == []
    call = ToolCall("c1", "Bash", None)
    assert len(normalizer.feed(call)) == 1
    assert normalizer.feed(call) == []


def test_deltas_name_their_item_and_split_assistant_text_from_command_output() -> None:
    out = normalize(
        "t1",
        [
            TextDelta("he", TextKind.ASSISTANT, "a1"),
            TextDelta("out", TextKind.TOOL_OUTPUT, "c1"),
            TextDelta("lost", TextKind.ASSISTANT, None),
            TextDelta("", TextKind.ASSISTANT, "a1"),
        ],
        DONE,
    )
    deltas = [e.delta for e in out if e.HasField("delta")]
    assert [
        (d.item_id, d.WhichOneof("body"), getattr(d, d.WhichOneof("body"))) for d in deltas
    ] == [
        ("a1", "text", "he"),
        ("c1", "command_output", "out"),
    ]


def test_cumulative_usage_reports_replace_each_other() -> None:
    out = normalize(
        "t1",
        [
            UsageReport(_usage(10, 1), None),
            UsageReport(_usage(25, 4), None),
            UsageReport(_usage(25, 4), None),
        ],
        DONE,
    )
    reports = [e.usage.usage for e in out if e.HasField("usage")]
    assert [(u.input_tokens, u.output_tokens) for u in reports] == [(10, 1), (25, 4)]
    assert out[-1].turn_ended.usage == reports[-1]


def test_usage_leaves_out_what_the_provider_does_not_report() -> None:
    bare = usage_proto(_usage(5, 2))
    assert not bare.HasField("cache_read_tokens")
    assert not bare.HasField("reasoning_tokens")
    assert not bare.HasField("cost_usd")
    full = usage_proto(
        ProviderUsage(
            tokens=TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=4,
                cache_write_input_tokens=2,
                reasoning_output_tokens=3,
            ),
            total_cost_usd=0.5,
        )
    )
    assert (
        full.cache_read_tokens,
        full.cache_creation_tokens,
        full.reasoning_tokens,
        full.cost_usd,
    ) == (4, 2, 3, 0.5)
    unknown = usage_proto(ProviderUsage(increment_known=False))
    assert not unknown.HasField("input_tokens")


def test_a_steer_is_accepted_when_consumed_and_refused_after_the_turn_ends_otherwise() -> None:
    book = SteerBook()
    book.add("m1", "one")
    book.add("m2", "two")
    book.add("m3", "two")
    normalizer = TurnNormalizer("t1", book)
    consumed = normalizer.feed(SteerConsumed("two"))
    assert [(e.steer.message_id, e.steer.accepted) for e in consumed] == [("m2", True)]
    normalizer.feed(SteerRejected("one", "turn ended"))  # answered at the end, after TurnEnded
    out = normalizer.finish(DONE)
    kinds = [e.WhichOneof("body") for e in out]
    assert kinds == ["turn_ended", "steer", "steer"]
    assert [(e.steer.message_id, e.steer.accepted) for e in out[1:]] == [
        ("m1", False),
        ("m3", False),
    ]


EVENT = st.one_of(
    st.builds(AssistantText, st.text(max_size=5), st.sampled_from(["a1", "a2", None])),
    st.builds(Reasoning, st.text(max_size=5), st.sampled_from(["r1", "r2", None])),
    st.builds(ToolCall, st.sampled_from(["c1", "c2"]), st.just("Bash"), st.none()),
    st.builds(
        ToolResult,
        st.sampled_from(["c1", "c2"]),
        st.just("Bash"),
        st.text(max_size=3),
        st.just(""),
        st.sampled_from([0, 1, None]),
        st.none(),
    ),
    st.builds(
        TextDelta,
        st.text(min_size=1, max_size=3),
        st.sampled_from(list(TextKind)),
        st.sampled_from(["a1", "c1", None]),
    ),
)


@given(
    st.lists(EVENT, max_size=20),
    st.sampled_from([DONE, orch.TURN_STATUS_INTERRUPTED, orch.TURN_STATUS_FAILED]),
)
def test_the_fold_is_deterministic_and_well_formed(events: list, status: int) -> None:
    first = normalize("t1", events, status)
    assert _bytes(first) == _bytes(normalize("t1", events, status))
    kinds = [e.WhichOneof("body") for e in first]
    assert kinds.count("turn_ended") == 1
    end = kinds.index("turn_ended")
    assert all(k == "steer" for k in kinds[end + 1 :])
    last: dict[str, int] = {}
    for e in first:
        if e.HasField("item"):
            last[e.item.id] = e.item.status
    assert orch.ITEM_STATUS_RUNNING not in last.values()


@given(st.lists(EVENT, max_size=20))
def test_feeding_the_events_twice_in_a_row_adds_only_deltas(events: list) -> None:
    normalizer = TurnNormalizer("t1")
    for event in events:
        normalizer.feed(event)
    again = [out for event in events for out in normalizer.feed(event)]
    assert all(e.WhichOneof("body") in {"delta", "item"} for e in again)
