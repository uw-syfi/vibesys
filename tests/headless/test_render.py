"""Tests for the headless terminal renderer (synthetic events in, text out)."""

from io import StringIO

from headless.render import HeadlessRenderer, TodoDisplay
from vibesys.api import (
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    RunConfiguredData,
    WorkspaceSnapshotData,
)
from vibesys.events import (
    AgentOutputChannel,
    AgentOutputChunkData,
    AgentStatusData,
    CoreEventData,
    CoreEventType,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    UsageUpdateData,
    make_core_event,
)


def _render(
    *payloads: tuple[CoreEventType, CoreEventData],
    color: bool = True,
    max_result_len: int | None = HeadlessRenderer.DEFAULT_MAX_RESULT_LEN,
    max_text_len: int | None = HeadlessRenderer.DEFAULT_MAX_TEXT_LEN,
) -> str:
    out = StringIO()
    renderer = HeadlessRenderer(
        out=out,
        color=color,
        max_result_len=max_result_len,
        max_text_len=max_text_len,
    )
    for event_type, data in payloads:
        renderer.handle(make_core_event(event_type, data=data))
    return out.getvalue()


def _render_event(
    event_type: CoreEventType,
    data: CoreEventData,
    *,
    status: EventStatus | None = None,
) -> str:
    out = StringIO()
    renderer = HeadlessRenderer(out=out)
    renderer.handle(make_core_event(event_type, data=data, status=status))
    return out.getvalue()


def _chunk(
    content: str,
    channel: AgentOutputChannel = "assistant",
    status: AgentStatusData | None = None,
) -> tuple[CoreEventType, CoreEventData]:
    return (
        CoreEventType.AGENT_OUTPUT_CHUNK,
        AgentOutputChunkData(channel=channel, content=content, status=status),
    )


_STATUS = AgentStatusData(
    agent_label="Implementer",
    elapsed_seconds=12.34,
    input_tokens=20_100,
    context_window=1_000_000,
)


class TestAssistantStreaming:
    def test_tokens_stream_without_newlines(self) -> None:
        assert _render(_chunk("hel"), _chunk("lo")) == "hello"

    def test_prefix_written_once_at_line_start(self) -> None:
        out = _render(_chunk("hel", status=_STATUS), _chunk("lo", status=_STATUS))
        assert out == "[Implementer | 12.3s | 20k/1.0M] hello"

    def test_no_prefix_for_anonymous_status(self) -> None:
        assert _render(_chunk("hi")) == "hi"

    def test_line_broken_before_next_surface(self) -> None:
        out = _render(_chunk("partial"), _chunk("diag\n", channel="diagnostic"))
        assert out == "partial\ndiag\n"


class TestAnalysisChannel:
    def test_rendered_as_prefixed_line(self) -> None:
        out = _render(_chunk("thinking hard", channel="analysis", status=_STATUS))
        assert out == "[Implementer | 12.3s | 20k/1.0M] thinking hard\n"


class TestBlockChannels:
    def test_diagnostic_rendered_verbatim(self) -> None:
        out = _render(_chunk("=== ROUND START ===\n", channel="diagnostic"))
        assert out == "=== ROUND START ===\n"

    def test_prompt_truncated_with_pointer_to_log(self) -> None:
        out = _render(_chunk("x" * 30 + "\n", channel="prompt"), max_text_len=20)
        assert out == "x" * 20 + "\n... [10 more chars, see log for full text]\n"

    def test_short_prompt_not_truncated(self) -> None:
        out = _render(_chunk("short\n", channel="prompt"), max_text_len=20)
        assert out == "short\n"


class TestFrameworkEvents:
    def test_gate_started_with_recipe_and_command(self) -> None:
        out = _render_event(
            CoreEventType.GATE_STARTED,
            GateStartedData.model_validate(
                {
                    "gate": "validation",
                    "recipe": "focused-tests",
                    "command": "uv run pytest -q",
                }
            ),
        )
        assert out == "[framework-validation] running focused-tests: uv run pytest -q\n"

    def test_gate_started_with_command_only(self) -> None:
        out = _render_event(
            CoreEventType.GATE_STARTED,
            GateStartedData.model_validate({"gate": "accuracy", "command": "trusted-check"}),
        )
        assert out == "[framework-accuracy] running: trusted-check\n"

    def test_gate_finished_with_metric_or_reused_recipe(self) -> None:
        metric = _render_event(
            CoreEventType.GATE_FINISHED,
            GateFinishedData.model_validate(
                {"gate": "benchmark", "metric": "tok_per_sec", "value": 42.0}
            ),
            status=EventStatus.COMPLETED,
        )
        reused = _render_event(
            CoreEventType.GATE_FINISHED,
            GateFinishedData.model_validate(
                {"gate": "validation", "recipe": "focused-tests", "reused": True}
            ),
            status=EventStatus.COMPLETED,
        )

        assert metric == "[framework-benchmark] PASS: tok_per_sec=42.0\n"
        assert reused == "[framework-validation] reused PASS: focused-tests\n"

    def test_failed_gate_carries_output_tail(self) -> None:
        out = _render_event(
            CoreEventType.GATE_FINISHED,
            GateFinishedData.model_validate(
                {"gate": "accuracy", "output_tail": "assertion mismatch"}
            ),
            status=EventStatus.FAILED,
        )
        assert out == "[framework-accuracy] FAIL: assertion mismatch\n"

    def test_workspace_snapshot_commit_and_no_change(self) -> None:
        committed = _render_event(
            CoreEventType.WORKSPACE_SNAPSHOT,
            WorkspaceSnapshotData(label="round-2", commit="a" * 40),
        )
        unchanged = _render_event(
            CoreEventType.WORKSPACE_SNAPSHOT,
            WorkspaceSnapshotData(label="round-3"),
        )

        assert committed == f"[git-tracking] snapshot 'round-2': {'a' * 12}\n"
        assert unchanged == "[git-tracking] no changes to commit for 'round-3'\n"

    def test_workspace_snapshot_baseline_and_exclusions(self) -> None:
        baseline = _render_event(
            CoreEventType.WORKSPACE_SNAPSHOT,
            WorkspaceSnapshotData(baseline="b" * 40),
        )
        excluded = _render_event(
            CoreEventType.WORKSPACE_SNAPSHOT,
            WorkspaceSnapshotData(excluded_paths=tuple(f"/p{i}" for i in range(7))),
        )

        assert baseline == f"[git-tracking] trusted input baseline: {'b' * 12}\n"
        assert excluded.startswith("[git-tracking] excluded 7 unreadable path(s)")
        assert "/p4" in excluded
        assert "/p5" not in excluded

    def test_run_configuration_header(self) -> None:
        out = _render_event(
            CoreEventType.RUN_CONFIGURED,
            RunConfiguredData(
                run_log_path="/logs/run.log",
                project_root="/work/project",
                objective="Make the queue fast.",
                search_policy="pareto-ucb",
                benchmark_contract=True,
            ),
        )
        assert out == (
            "[log] run log: /logs/run.log\n"
            "[log] project root: /work/project\n"
            "[log] objective: Make the queue fast.\n"
            "[log] search policy: pareto-ucb\n"
            "[log] benchmark result contract declared; it owns candidate fitness\n"
        )

    def test_framework_warning_with_and_without_detail(self) -> None:
        detailed = _render_event(
            CoreEventType.FRAMEWORK_WARNING,
            FrameworkWarningData(summary="profiler failed", detail="boom"),
        )
        bare = _render_event(
            CoreEventType.FRAMEWORK_WARNING,
            FrameworkWarningData(summary="odd state"),
        )

        assert detailed == "[warn] profiler failed: boom\n"
        assert bare == "[warn] odd state\n"


class TestToolEvents:
    def test_tool_channel_chunks_are_ignored(self) -> None:
        # Tool traffic renders from the typed events; tool-channel chunks
        # (legacy event files only) must not double-render.
        assert _render(_chunk("→ shell({})\n", channel="tool")) == ""

    def test_tool_call_line(self) -> None:
        out = _render(
            (
                CoreEventType.TOOL_CALL,
                ToolCallData(tool="shell", args={"cmd": "ls"}, status=_STATUS),
            )
        )
        assert out == '\n[Implementer | 12.3s | 20k/1.0M] → shell(cmd="ls")\n'

    def test_tool_call_args_truncated(self) -> None:
        long = "x" * 200
        out = _render((CoreEventType.TOOL_CALL, ToolCallData(tool="shell", args={"cmd": long})))
        assert long not in out
        assert "x" * 80 + "..." in out

    def test_non_string_args_rendered_as_json(self) -> None:
        out = _render((CoreEventType.TOOL_CALL, ToolCallData(tool="t", args={"n": 3})))
        assert "n=3" in out

    def test_tool_result_indented(self) -> None:
        out = _render((CoreEventType.TOOL_RESULT, ToolResultData(tool="shell", content="a\nb")))
        assert out == "  a\n  b\n"

    def test_tool_result_truncated(self) -> None:
        out = _render(
            (CoreEventType.TOOL_RESULT, ToolResultData(tool="shell", content="a" * 30)),
            max_result_len=10,
        )
        assert out == "  " + "a" * 10 + "...\n"


class TestTodoRendering:
    def _todos(self) -> list[TodoItemData]:
        return [
            TodoItemData(content="Set up project", status="completed"),
            TodoItemData(content="Implement handlers", status="in_progress"),
            TodoItemData(content="Add tests", status="pending"),
        ]

    def test_renders_box_with_items(self) -> None:
        out = _render((CoreEventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())))
        assert "┌─ Todo" in out
        assert "Set up project" in out
        assert "Implement handlers" in out
        assert "Add tests" in out

    def test_status_indicators(self) -> None:
        out = _render((CoreEventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())))
        assert "✓" in out  # completed
        assert "▶" in out  # in_progress
        assert "○" in out  # pending

    def test_unknown_status_degrades(self) -> None:
        todos = [TodoItemData(content="odd", status="unknown-state")]
        out = _render((CoreEventType.TODO_UPDATE, TodoUpdateData(todos=todos)))
        assert "? odd" in out

    def test_plain_mode_has_no_ansi_colors(self) -> None:
        out = _render((CoreEventType.TODO_UPDATE, TodoUpdateData(todos=self._todos())), color=False)
        assert "\033[3" not in out  # no color codes
        assert "✓" in out


class TestTodoDisplay:
    def test_clears_previous_lines(self) -> None:
        buf = StringIO()
        td = TodoDisplay(file=buf)
        td.update([TodoItemData(content="task one", status="pending")])
        buf.truncate(0)
        buf.seek(0)
        td.update(
            [
                TodoItemData(content="task one", status="completed"),
                TodoItemData(content="task two", status="pending"),
            ]
        )
        second_output = buf.getvalue()
        # Should contain ANSI cursor-up escape to overwrite previous block
        assert "\033[" in second_output
        assert "A" in second_output

    def test_empty_list_prints_nothing(self) -> None:
        buf = StringIO()
        TodoDisplay(file=buf).update([])
        assert buf.getvalue() == ""


class TestIgnoredEvents:
    def test_usage_update_produces_no_output(self) -> None:
        out = _render((CoreEventType.USAGE_UPDATE, UsageUpdateData(input_tokens=5)))
        assert out == ""

    def test_event_without_data_produces_no_output(self) -> None:
        out = StringIO()
        renderer = HeadlessRenderer(out=out)
        renderer.handle(make_core_event(CoreEventType.RUN_STARTED))
        assert out.getvalue() == ""
