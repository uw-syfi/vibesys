"""Text the run did not author reaches the terminal as text, never as a command.

Agent output, tool arguments and results, todo items, and a failed gate's
output tail all come from a model or a candidate program. A control character
in any of them, written raw, is a terminal command: ESC moves the cursor,
erases the screen, or writes the window title or the clipboard (OSC 52), and
a carriage return rewrites the current line, so a later line can be dressed
as an earlier ``PASS``.
"""

from __future__ import annotations

import re
from io import StringIO

from hypothesis import example, given, settings
from hypothesis import strategies as st

from headless.render import HeadlessRenderer
from vibesys.api import EventStatus, FrameworkWarningData, GateFinishedData, QuotaPausedData
from vibesys.events import (
    AgentOutputChunkData,
    AgentStatusData,
    CoreEventData,
    CoreEventType,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    make_core_event,
)
from vs_sim.api.testing import examples

# Every C0 and C1 control, DEL, and plain text around them.
_TEXT = st.text(
    alphabet=st.one_of(
        st.characters(min_codepoint=0, max_codepoint=0x9F),
        st.sampled_from("ab ✓水\U0001d11e"),
    ),
    max_size=40,
)
_STATUS = st.none() | st.builds(
    AgentStatusData, agent_label=_TEXT, progress=_TEXT, elapsed_seconds=st.just(1.0)
)
_PAYLOADS: st.SearchStrategy[tuple[CoreEventType, CoreEventData, EventStatus | None]] = st.one_of(
    st.builds(
        lambda channel, content, status: (
            CoreEventType.AGENT_OUTPUT_CHUNK,
            AgentOutputChunkData(channel=channel, content=content, status=status),
            None,
        ),
        st.sampled_from(["assistant", "analysis", "prompt", "diagnostic"]),
        _TEXT,
        _STATUS,
    ),
    st.builds(
        lambda tool, args, status: (
            CoreEventType.TOOL_CALL,
            ToolCallData(tool=tool, args=args, status=status),
            None,
        ),
        _TEXT,
        st.dictionaries(_TEXT, _TEXT | st.integers(), max_size=3),
        _STATUS,
    ),
    st.builds(
        lambda tool, content: (
            CoreEventType.TOOL_RESULT,
            ToolResultData(tool=tool, content=content),
            None,
        ),
        _TEXT,
        _TEXT,
    ),
    st.builds(
        lambda contents: (
            CoreEventType.TODO_UPDATE,
            TodoUpdateData(
                todos=[TodoItemData(content=text, status="pending") for text in contents]
            ),
            None,
        ),
        st.lists(_TEXT, min_size=1, max_size=3),
    ),
    st.builds(
        lambda recipe, tail: (
            CoreEventType.GATE_FINISHED,
            GateFinishedData.model_validate(
                {"gate": "benchmark", "recipe": recipe, "output_tail": tail}
            ),
            EventStatus.FAILED,
        ),
        _TEXT,
        _TEXT,
    ),
    st.builds(
        lambda summary, detail: (
            CoreEventType.FRAMEWORK_WARNING,
            FrameworkWarningData(summary=summary, detail=detail),
            None,
        ),
        _TEXT,
        _TEXT,
    ),
    st.builds(
        lambda provider, detail: (
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(provider=provider, condition="rate_limited", detail=detail),
            None,
        ),
        _TEXT,
        _TEXT,
    ),
)
# What the renderer itself writes: colors, and the todo box's cursor-up and
# erase-line commands.
_OWN_SEQUENCES = re.compile(r"\x1b\[[0-9;]*[mAK]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@settings(max_examples=examples(pr=50, nightly=3000))
@given(payloads=st.lists(_PAYLOADS, min_size=1, max_size=6), color=st.booleans())
@example(
    payloads=[
        (
            CoreEventType.TOOL_RESULT,
            ToolResultData(tool="shell", content="FAIL\r[framework-benchmark] PASS"),
            None,
        )
    ],
    color=False,
)
@example(
    payloads=[
        (
            CoreEventType.AGENT_OUTPUT_CHUNK,
            AgentOutputChunkData(channel="assistant", content="\x1b]52;c;cm0gLXJmIH4=\x07"),
            None,
        )
    ],
    color=True,
)
def test_no_control_character_from_event_text_reaches_the_terminal(
    payloads: list[tuple[CoreEventType, CoreEventData, EventStatus | None]], *, color: bool
) -> None:
    """Every control the output holds is one the renderer wrote itself.

    Tab and newline are text. A CRLF line ending stays a line ending; every
    other control is shown as its ``\\xNN`` spelling.
    """
    out = StringIO()
    renderer = HeadlessRenderer(out=out, color=color, max_result_len=None, max_text_len=None)

    for event_type, data, status in payloads:
        renderer.handle(make_core_event(event_type, data=data, status=status))

    assert not _CONTROL.findall(_OWN_SEQUENCES.sub("", out.getvalue()))


def test_a_control_character_is_shown_not_dropped() -> None:
    out = StringIO()
    renderer = HeadlessRenderer(out=out, color=False)

    renderer.handle(
        make_core_event(
            CoreEventType.TOOL_RESULT,
            data=ToolResultData(tool="shell", content="a\x1b[2Jb\r\nc\td"),
        )
    )

    assert out.getvalue() == "  a\\x1b[2Jb\n  c\td\n"
