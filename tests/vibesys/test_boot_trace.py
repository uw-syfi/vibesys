"""Unit tests for the boot-trace span facility.

``vibesys.boot_trace`` times the boot stages that run before a ``RunLogger``
exists (``main.py``'s dispatch preamble, ``context.py``'s assembly) and
buffers their lines until a consumer drains them into the run log. See
``tests/vibesys/test_context.py`` for the integration test that exercises the drain
through a real ``open_run_resources`` call.
"""

import pytest

from vibesys import boot_trace


@pytest.fixture(autouse=True)
def quiet_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with the trace off and nothing left buffered."""
    monkeypatch.delenv(boot_trace.BOOT_TRACE_ENV, raising=False)
    boot_trace.drain_log_lines()


def test_span_records_one_line_per_span() -> None:
    with boot_trace.span("parse_cli_invocation"):
        pass

    lines = boot_trace.drain_log_lines()
    assert len(lines) == 1
    assert lines[0].startswith("boot span parse_cli_invocation: ")
    assert lines[0].endswith("ms")


def test_nested_spans_qualify_names_and_report_children_first() -> None:
    with boot_trace.span("dispatch"):
        with boot_trace.span("parse_cli_invocation"):
            pass
        with boot_trace.span("run_started_event"):
            pass

    names = [line.split(":")[0] for line in boot_trace.drain_log_lines()]
    assert names == [
        "boot span dispatch.parse_cli_invocation",
        "boot span dispatch.run_started_event",
        "boot span dispatch",
    ]


def test_sibling_spans_after_a_nest_are_not_qualified_by_it() -> None:
    """The stack unwinds, so a later span is not a child of an earlier one."""
    with boot_trace.span("dispatch"), boot_trace.span("parse_cli_invocation"):
        pass
    with boot_trace.span("context"):
        pass

    names = [line.split(":")[0] for line in boot_trace.drain_log_lines()]
    assert names == [
        "boot span dispatch.parse_cli_invocation",
        "boot span dispatch",
        "boot span context",
    ]


def test_a_raising_span_is_recorded_and_the_exception_propagates() -> None:
    def open_project() -> None:
        with boot_trace.span("project_open"):
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"), boot_trace.span("context"):
        open_project()

    lines = boot_trace.drain_log_lines()
    assert [line.split(":")[0] for line in lines] == [
        "boot span context.project_open",
        "boot span context",
    ]
    assert all(line.endswith("(raised ValueError)") for line in lines)


def test_traced_decorator_times_the_whole_call() -> None:
    @boot_trace.traced("load_config_and_skills")
    def load(value: int) -> int:
        return value + 1

    assert load(1) == 2

    lines = boot_trace.drain_log_lines()
    assert len(lines) == 1
    assert lines[0].startswith("boot span load_config_and_skills: ")


def test_drain_clears_the_buffer() -> None:
    with boot_trace.span("first"):
        pass
    boot_trace.drain_log_lines()
    with boot_trace.span("second"):
        pass

    assert [line.split(":")[0] for line in boot_trace.drain_log_lines()] == ["boot span second"]


def test_spans_are_silent_on_stderr_by_default(capfd: pytest.CaptureFixture[str]) -> None:
    with boot_trace.span("context"):
        pass

    assert capfd.readouterr().err == ""
    assert boot_trace.drain_log_lines() != []  # still recorded for the run log


def test_boot_trace_env_echoes_spans_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(boot_trace.BOOT_TRACE_ENV, "1")

    with boot_trace.span("context"):
        pass

    assert "boot span context: " in capfd.readouterr().err
    assert boot_trace.drain_log_lines() != []


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_exact_value_one_enables_the_stderr_echo(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(boot_trace.BOOT_TRACE_ENV, value)

    with boot_trace.span("context"):
        pass

    assert capfd.readouterr().err == ""
    assert boot_trace.trace_enabled() is False


def test_child_env_carries_the_launch_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(boot_trace.BOOT_TRACE_ENV, raising=False)
    boot_trace.mark_launch()

    env = boot_trace.child_env()

    assert set(env) == {boot_trace.LAUNCH_START_ENV}
    assert int(env[boot_trace.LAUNCH_START_ENV]) > 0


def test_child_env_anchors_even_without_mark_launch() -> None:
    """A spawn that skipped ``mark_launch`` still ships an anchor."""
    assert int(boot_trace.child_env()[boot_trace.LAUNCH_START_ENV]) > 0


def test_child_env_propagates_the_trace_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(boot_trace.BOOT_TRACE_ENV, "1")

    assert boot_trace.child_env()[boot_trace.BOOT_TRACE_ENV] == "1"
