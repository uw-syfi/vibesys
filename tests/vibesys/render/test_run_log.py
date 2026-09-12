"""Tests for the run-log rendering of typed framework events."""

from __future__ import annotations

import io

from vibesys.render.run_log import RunLogRenderer, format_framework_event
from vibesys.run.events import (
    AgentOutputChunkData,
    CoreEventType,
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateKind,
    GateStartedData,
    RunConfiguredData,
    WorkspaceSnapshotData,
    make_core_event,
)


def _event(event_type, data, status=None):  # noqa: ANN001, ANN202
    return make_core_event(event_type, data=data, status=status)


class TestFormatFrameworkEvent:
    def test_gate_started_with_recipe_and_command(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.GATE_STARTED,
                GateStartedData(
                    gate=GateKind.VALIDATION,
                    recipe="focused-tests",
                    command="uv run pytest -q",
                ),
                status=EventStatus.ACTIVE,
            )
        )
        assert line == "[framework-validation] running focused-tests: uv run pytest -q"

    def test_gate_started_command_only(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.GATE_STARTED,
                GateStartedData(gate=GateKind.ACCURACY, command="trusted-check"),
            )
        )
        assert line == "[framework-accuracy] running: trusted-check"

    def test_gate_finished_pass_with_metric(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.GATE_FINISHED,
                GateFinishedData(gate=GateKind.BENCHMARK, metric="tok_per_sec", value=42.0),
                status=EventStatus.COMPLETED,
            )
        )
        assert line == "[framework-benchmark] PASS: tok_per_sec=42.0"

    def test_gate_finished_reused_pass(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.GATE_FINISHED,
                GateFinishedData(gate=GateKind.VALIDATION, recipe="focused-tests", reused=True),
                status=EventStatus.COMPLETED,
            )
        )
        assert line == "[framework-validation] reused PASS: focused-tests"

    def test_gate_finished_failure_carries_the_output_tail(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.GATE_FINISHED,
                GateFinishedData(gate=GateKind.ACCURACY, output_tail="assertion mismatch"),
                status=EventStatus.FAILED,
            )
        )
        assert line == "[framework-accuracy] FAIL: assertion mismatch"

    def test_workspace_snapshot_commit_and_no_change(self):  # noqa: ANN201
        committed = format_framework_event(
            _event(
                CoreEventType.WORKSPACE_SNAPSHOT,
                WorkspaceSnapshotData(label="round-2", commit="a" * 40),
            )
        )
        assert committed == f"[git-tracking] snapshot 'round-2': {'a' * 12}"
        unchanged = format_framework_event(
            _event(CoreEventType.WORKSPACE_SNAPSHOT, WorkspaceSnapshotData(label="round-3"))
        )
        assert unchanged == "[git-tracking] no changes to commit for 'round-3'"

    def test_workspace_snapshot_baseline_and_exclusions(self):  # noqa: ANN201
        baseline = format_framework_event(
            _event(CoreEventType.WORKSPACE_SNAPSHOT, WorkspaceSnapshotData(baseline="b" * 40))
        )
        assert baseline == f"[git-tracking] trusted input baseline: {'b' * 12}"
        excluded = format_framework_event(
            _event(
                CoreEventType.WORKSPACE_SNAPSHOT,
                WorkspaceSnapshotData(excluded_paths=tuple(f"/p{i}" for i in range(7))),
            )
        )
        assert excluded is not None
        assert excluded.startswith("[git-tracking] excluded 7 unreadable path(s)")
        assert "/p4" in excluded
        assert "/p5" not in excluded

    def test_run_configured_renders_the_header_block(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.RUN_CONFIGURED,
                RunConfiguredData(
                    run_log_path="/logs/run.log",
                    project_root="/work/project",
                    objective="Make the queue fast.",
                    search_policy="pareto-ucb",
                    benchmark_contract=True,
                ),
            )
        )
        assert line == (
            "[log] run log: /logs/run.log\n"
            "[log] project root: /work/project\n"
            "[log] objective: Make the queue fast.\n"
            "[log] search policy: pareto-ucb\n"
            "[log] benchmark result contract declared; it owns candidate fitness"
        )

    def test_framework_warning(self):  # noqa: ANN201
        with_detail = format_framework_event(
            _event(
                CoreEventType.FRAMEWORK_WARNING,
                FrameworkWarningData(summary="profiler failed", detail="boom"),
            )
        )
        assert with_detail == "[warn] profiler failed: boom"
        bare = format_framework_event(
            _event(CoreEventType.FRAMEWORK_WARNING, FrameworkWarningData(summary="odd state"))
        )
        assert bare == "[warn] odd state"

    def test_non_framework_events_render_nothing(self):  # noqa: ANN201
        line = format_framework_event(
            _event(
                CoreEventType.AGENT_OUTPUT_CHUNK,
                AgentOutputChunkData(channel="assistant", content="hi"),
            )
        )
        assert line is None


class TestRunLogRenderer:
    def test_writes_framework_lines_to_the_writer(self):  # noqa: ANN201
        buffer = io.StringIO()
        renderer = RunLogRenderer(buffer)
        renderer.handle(
            _event(
                CoreEventType.FRAMEWORK_WARNING,
                FrameworkWarningData(summary="profiler failed", detail="boom"),
            )
        )
        renderer.handle(
            _event(
                CoreEventType.AGENT_OUTPUT_CHUNK,
                AgentOutputChunkData(channel="assistant", content="ignored"),
            )
        )
        assert buffer.getvalue() == "[warn] profiler failed: boom\n"

    def test_closed_writer_is_left_alone(self):  # noqa: ANN201
        buffer = io.StringIO()
        renderer = RunLogRenderer(buffer)
        buffer.close()
        renderer.handle(
            _event(CoreEventType.FRAMEWORK_WARNING, FrameworkWarningData(summary="late"))
        )
