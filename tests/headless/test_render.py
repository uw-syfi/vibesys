"""Tests for the headless terminal renderer (synthetic events in, text out)."""

from datetime import UTC, datetime
from io import StringIO

from headless.render import HeadlessRenderer, TodoDisplay
from vibesys.api import (
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    InputMeasurementFailedData,
    InputMeasurementFailureKind,
    MeasurementComparedData,
    ProviderSwitchedData,
    QuotaAbandonedData,
    QuotaPausedData,
    QuotaResumedData,
    RateLimitUpdateData,
    RunConfiguredData,
    WinnerSelectedData,
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
from vs_core.api import ComparisonVerdict


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
        renderer.handle(
            make_core_event(event_type, data=data, timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        )
    return out.getvalue()


def _render_event(
    event_type: CoreEventType,
    data: CoreEventData,
    *,
    status: EventStatus | None = None,
) -> str:
    out = StringIO()
    renderer = HeadlessRenderer(out=out)
    renderer.handle(
        make_core_event(
            event_type, data=data, status=status, timestamp=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
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

    def test_winner_selection_names_the_result(self) -> None:
        candidate = _render_event(
            CoreEventType.WINNER_SELECTED,
            WinnerSelectedData(selection="candidate", revision="c" * 40, settlement_id="s-1"),
        )
        baseline = _render_event(
            CoreEventType.WINNER_SELECTED,
            WinnerSelectedData(selection="baseline", revision="b" * 40),
        )

        assert candidate == f"[result] selected trusted winner {'c' * 12} (settlement s-1)\n"
        assert baseline.startswith("[result] selected the trusted input unchanged")

    def test_a_failed_input_measurement_names_how_the_stage_and_what_it_printed(self) -> None:
        out = _render_event(
            CoreEventType.INPUT_MEASUREMENT_FAILED,
            InputMeasurementFailedData(
                failure=InputMeasurementFailureKind.ACCURACY_FAILED,
                stage="correctness",
                summary="accuracy stage exceeded its 1200 s limit",
            ),
        )
        bare = _render_event(
            CoreEventType.INPUT_MEASUREMENT_FAILED,
            InputMeasurementFailedData(failure=InputMeasurementFailureKind.READINGS_UNDECODABLE),
        )

        assert out.startswith(
            "[input] the input's trusted measurement failed (accuracy-failed, stage correctness)"
        )
        assert out.endswith(":\naccuracy stage exceeded its 1200 s limit\n")
        assert bare.startswith(
            "[input] the input's trusted measurement failed (readings-undecodable)"
        )
        assert bare.count("\n") == 1

    def test_a_comparison_states_the_verdict_the_values_and_the_sample_counts(self) -> None:
        out = _render_event(
            CoreEventType.MEASUREMENT_COMPARED,
            MeasurementComparedData(
                metric="throughput",
                unit="tok/s",
                direction="max",
                candidate_commit="c" * 40,
                base_commit="b" * 40,
                candidate_value=130.0,
                base_value=100.0,
                ratio=1.3,
                noise=0.0283,
                verdict=ComparisonVerdict.BETTER,
                candidate_samples=2,
                base_samples=1,
            ),
        )

        assert out == (
            f"[compare] {'c' * 12} vs base {'b' * 12}: better, 130 tok/s vs 100 tok/s, "
            "ratio 1.300 \u00b1 0.028 (2 and 1 samples)\n"
        )

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


class TestRateLimitEvents:
    def test_an_exhausted_window_is_shown_with_its_reset_time(self) -> None:
        out = _render_event(
            CoreEventType.RATE_LIMIT_UPDATE,
            RateLimitUpdateData(
                provider="claude", window="five_hour", resets_at=1_791_954_019, exhausted=True
            ),
        )
        assert (
            out
            == "[rate-limit] claude five_hour window is exhausted; resets 2026-10-14 05:00 UTC\n"
        )

    def test_a_window_with_capacity_left_is_not_shown(self) -> None:
        out = _render_event(
            CoreEventType.RATE_LIMIT_UPDATE,
            RateLimitUpdateData(provider="claude", window="five_hour", exhausted=False),
        )
        assert out == ""


class TestQuotaEvents:
    def test_a_quota_pause_names_the_provider_the_diagnostic_and_the_reset_time(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(
                provider="claude",
                condition="quota_exhausted",
                detail="You've hit your limit",
                resets_at=1_791_954_019,
            ),
        )
        assert out == (
            "[quota] claude quota exhausted: You've hit your limit; the run is paused "
            "(capacity returns 2026-10-14 05:00 UTC)\n"
        )

    def test_sustained_rate_limiting_without_a_reset_time_omits_it(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(provider="codex", condition="rate_limited", detail="429"),
        )
        assert out == "[quota] codex rate limited: 429; the run is paused\n"

    def test_a_waiting_policy_says_when_the_run_resumes_by_itself(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(
                provider="claude",
                condition="quota_exhausted",
                detail="limit",
                resumes_at=1_791_954_019,
            ),
        )
        assert out == (
            "[quota] claude quota exhausted: limit; the run is paused; "
            "resuming by itself at 2026-10-14 05:00 UTC\n"
        )

    def test_a_resume_is_shown(self) -> None:
        out = _render_event(CoreEventType.QUOTA_RESUMED, QuotaResumedData(provider="claude"))
        assert out == "[quota] resumed; sending the paused claude turn again\n"

    def test_a_wait_that_elapsed_is_shown(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_RESUMED, QuotaResumedData(provider="claude", reason="wait_elapsed")
        )
        assert out == "[quota] the wait elapsed; sending the paused claude turn again\n"

    def test_an_outage_says_only_the_turn_waits(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(
                provider="claude",
                condition="unavailable",
                detail="503",
                resumes_at=1_791_954_019,
                policy="wait",
            ),
        )
        assert out == (
            "[quota] claude unavailable: 503; this turn waits, the run goes on; "
            "trying again at 2026-10-14 05:00 UTC\n"
        )

    def test_an_outage_resume_shows_how_long_the_turn_waited(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_RESUMED,
            QuotaResumedData(
                provider="claude",
                condition="unavailable",
                reason="wait_elapsed",
                waited_seconds=600.0,
            ),
        )
        assert out == "[quota] claude turn waited 10 min out of an outage; sending it again\n"

    def test_the_run_end_reports_the_total_outage_wait(self) -> None:
        """Every outage wait counts, including the last wait of a turn it ended (a cancel,
        the run's deadline), which publishes ``quota_abandoned`` instead of a resume."""
        out = StringIO()
        renderer = HeadlessRenderer(out=out)
        resumed = ((3600.0, "unavailable"), (60.0, "unavailable"), (900.0, None))
        for waited, condition in resumed:
            renderer.handle(
                make_core_event(
                    CoreEventType.QUOTA_RESUMED,
                    data=QuotaResumedData(
                        provider="claude",
                        condition=condition,
                        reason="wait_elapsed",
                        waited_seconds=waited,
                    ),
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        for waited, condition in ((65.0, "unavailable"), (900.0, "rate_limited")):
            renderer.handle(
                make_core_event(
                    CoreEventType.QUOTA_ABANDONED,
                    data=QuotaAbandonedData(
                        provider="claude",
                        condition=condition,
                        detail="503",
                        reason="the turn was cancelled after 1 min 5 s",
                        waited_seconds=waited,
                    ),
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        renderer.finish()

        assert out.getvalue().splitlines()[-1] == (
            "[quota] turns waited out provider outages for 1 h 2 min 5 s in total"
        )

    def test_a_run_without_an_outage_reports_none(self) -> None:
        out = StringIO()
        HeadlessRenderer(out=out).finish()
        assert out.getvalue() == ""

    def test_an_abandoned_turn_names_the_policys_reason(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_ABANDONED,
            QuotaAbandonedData(
                provider="codex",
                condition="rate_limited",
                detail="429",
                reason="the quota policy is fail",
            ),
        )
        assert out == "[quota] codex rate limited: 429; the quota policy is fail\n"


class TestProviderSwitch:
    def test_a_pause_names_the_fallback_an_operator_may_choose(self) -> None:
        out = _render_event(
            CoreEventType.QUOTA_PAUSED,
            QuotaPausedData(
                provider="claude",
                condition="quota_exhausted",
                detail="limit",
                fallback_provider="codex",
                fallback_model="gpt-5",
            ),
        )
        assert out == (
            "[quota] claude quota exhausted: limit; the run is paused; fallback: codex (gpt-5)\n"
        )

    def test_a_switch_names_both_providers_the_model_and_who_chose_it(self) -> None:
        out = _render_event(
            CoreEventType.PROVIDER_SWITCHED,
            ProviderSwitchedData(
                from_provider="claude",
                to_provider="codex",
                to_model="gpt-5",
                reason="policy",
                detail="waited 3600s for capacity, the whole budget",
            ),
        )
        assert out == (
            "[quota] switched from claude to codex (gpt-5) by the policy; new sessions start "
            "there (waited 3600s for capacity, the whole budget)\n"
        )


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
        renderer.handle(
            make_core_event(CoreEventType.RUN_STARTED, timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        )
        assert out.getvalue() == ""
