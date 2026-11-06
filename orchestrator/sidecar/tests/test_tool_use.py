"""A tool call becomes exactly one typed call, for both harnesses, and nothing raw reaches a client.

The corpus is the argument shapes of real Claude Code tool blocks from live runs and of the Codex
items agentshim turns into tool calls, with every value replaced by a placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

import protovalidate
import pytest
from agentshim import ToolCall, ToolResult
from hypothesis import given
from hypothesis import strategies as st

from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.normalize import normalize
from vs_sidecar.tool_use import describe_call

DONE = orch.TURN_STATUS_COMPLETED
CASES = {"shell", "read", "write", "edit", "search", "other"}

CLAUDE_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Grep", "Glob", "Skill", "Monitor"]
CLAUDE_TOOLS += ["mcp__vibesys__post_handoff", "ToolSearch"]
CODEX_TOOLS = ["execute", "exec_command", "shell", "file_change", "apply_patch", "web_search"]
CODEX_TOOLS += ["mcp_tool_call", "dynamic_tool_call"]

_WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8)
_PLAIN = st.text(alphabet="abcdefghij /._-", max_size=12)
_SCALAR = st.one_of(_PLAIN, st.integers(-5, 5), st.booleans(), st.none())
_VALUE = st.one_of(
    _SCALAR, st.lists(_SCALAR, max_size=3), st.dictionaries(_WORD, _SCALAR, max_size=2)
)
_KNOWN_KEYS = st.sampled_from(
    ["command", "cmd", "file_path", "path", "pattern", "query", "changes"]
)
ARGS = st.one_of(
    st.none(),
    _PLAIN,
    st.dictionaries(st.one_of(_WORD, _KNOWN_KEYS), _VALUE, max_size=4),
)


def _calls(tools: list[str]) -> st.SearchStrategy[ToolCall]:
    return st.builds(
        ToolCall,
        st.text(alphabet="cd0123", min_size=1, max_size=3),
        st.one_of(st.sampled_from(tools), _WORD),
        ARGS,
    )


def _tool_items(events: list) -> dict[str, sc.SessionItem]:
    last: dict[str, sc.SessionItem] = {}
    for event in normalize("t1", events, DONE):
        if event.HasField("item") and event.item.WhichOneof("body") == "tool":
            last[event.item.id] = event.item
    return last


@pytest.mark.parametrize("tools", [CLAUDE_TOOLS, CODEX_TOOLS], ids=["claude", "codex"])
@given(data=st.data())
def test_every_call_becomes_exactly_one_typed_call(tools: list[str], data: st.DataObject) -> None:
    calls = data.draw(st.lists(_calls(tools), max_size=8))
    items = _tool_items(calls)
    assert set(items) == {c.tool_id for c in calls}
    validator = protovalidate.Validator()
    for item in items.values():
        assert item.tool.WhichOneof("call") in CASES
        validator.validate(item.tool)


@pytest.mark.parametrize("tools", [CLAUDE_TOOLS, CODEX_TOOLS], ids=["claude", "codex"])
@given(data=st.data())
def test_a_result_keeps_the_call_it_completes(tools: list[str], data: st.DataObject) -> None:
    call = data.draw(_calls(tools))
    result = ToolResult(call.tool_id, call.tool, "out", "", 0, 0.0)
    started, finished = _tool_items([call]), _tool_items([call, result])
    assert finished[call.tool_id].tool.WhichOneof("call") == started[call.tool_id].tool.WhichOneof(
        "call"
    )
    assert finished[call.tool_id].tool.output == "out"


@given(st.sampled_from(CLAUDE_TOOLS + CODEX_TOOLS), ARGS)
def test_a_call_is_a_function_of_its_name_and_arguments(tool: str, args: object) -> None:
    assert describe_call(tool, args) == describe_call(tool, args)


@given(_WORD, st.dictionaries(_WORD, _SCALAR, min_size=1, max_size=4))
def test_an_unknown_tool_is_shown_by_its_name_and_one_argument_to_a_line(
    name: str, args: dict[str, object]
) -> None:
    key = f"zz_{name}"  # not a name any harness uses
    call = describe_call(key, args)
    assert call.WhichOneof("call") == "other"
    assert call.other.name == key
    assert len(call.other.text.split("\n")) >= len(args)
    for field, value in args.items():
        if isinstance(value, str):
            assert f"{field}: {value}" in call.other.text


@given(st.sampled_from(CLAUDE_TOOLS + CODEX_TOOLS), ARGS)
def test_no_part_of_a_call_is_the_json_the_provider_received(tool: str, args: object) -> None:
    if not isinstance(args, dict) or not args:
        return
    raw = {
        json.dumps(args),
        json.dumps(args, sort_keys=True),
        json.dumps(args, separators=(",", ":")),
    }
    call = describe_call(tool, args)
    shown = {
        call.other.text,
        call.other.name,
        call.shell.command,
        call.read.target,
        call.search.pattern,
    }
    assert not shown & raw


def test_a_tool_that_has_not_said_what_it_is_asked_is_still_its_kind() -> None:
    assert describe_call("Bash", None).WhichOneof("call") == "shell"
    assert describe_call("Read", {}).WhichOneof("call") == "read"
    assert describe_call("Edit", {}).WhichOneof("call") == "edit"
    assert describe_call("", None).other.name == "unknown"


def test_a_wrapped_codex_tool_is_named_by_the_tool_it_wraps() -> None:
    call = describe_call(
        "mcp_tool_call", {"server": "s", "tool": "post_handoff", "arguments": {"claim": "x"}}
    )
    assert (call.other.name, call.other.text) == ("post_handoff", "claim: x")


CORPUS = json.loads((Path(__file__).parent / "corpus" / "tool_shapes.json").read_text())


@pytest.mark.parametrize(
    "shape", CORPUS, ids=lambda s: f"{s['harness']}-{s['tool']}-{len(s['args'] or '')}"
)
def test_every_shape_of_a_real_frame_maps_to_its_case(shape: dict) -> None:
    items = _tool_items([ToolCall("c1", shape["tool"], shape["args"])])
    assert items["c1"].tool.WhichOneof("call") == shape["case"]
    protovalidate.Validator().validate(items["c1"].tool)


def test_the_corpus_covers_both_harnesses_and_names_what_is_shown_generically() -> None:
    assert {s["harness"] for s in CORPUS} == {"claude", "codex"}
    generic = sorted({s["tool"] for s in CORPUS if s["case"] == "other"})
    assert "mcp__vibesys__post_handoff" in generic
    assert not {"Bash", "Read", "Edit", "Write", "Grep", "Glob", "execute"} & set(generic)
