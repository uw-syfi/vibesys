"""Tests for bounded, stream-aware sandbox execution results."""

from __future__ import annotations

import pytest

from vs_sandbox.execution import bounded_execution_result


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code"),
    [
        ("", "", 0),
        ("stdout", "", 0),
        ("", "stderr", 1),
        ("stdout", "stderr", 1),
    ],
)
def test_output_below_or_at_cap_is_unchanged(
    stdout: str,
    stderr: str,
    exit_code: int,
) -> None:
    result = bounded_execution_result(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        max_output_chars=len(stdout + stderr),
    )

    assert result.output == stdout + stderr
    assert result.stdout == stdout
    assert result.stderr == stderr
    assert not result.truncated


def test_successful_truncation_keeps_the_combined_prefix() -> None:
    result = bounded_execution_result(
        stdout="s" * 100,
        stderr="diagnostic",
        exit_code=0,
        max_output_chars=60,
    )

    assert len(result.output) == 60
    assert result.stdout.startswith("s")
    assert result.stderr == ""
    assert "diagnostic" not in result.output
    assert "truncated" in result.output


def test_failed_truncation_keeps_stdout_prefix_and_short_stderr_tail() -> None:
    result = bounded_execution_result(
        stdout="s" * 100,
        stderr="fatal compiler error\n",
        exit_code=1,
        max_output_chars=80,
    )

    assert len(result.output) == 80
    assert result.stdout.startswith("s")
    assert result.stderr.endswith("fatal compiler error\n")
    assert result.output == result.stdout + result.stderr


def test_failed_stderr_only_truncation_keeps_its_tail() -> None:
    stderr = "old diagnostics\n" * 20 + "fatal tail\n"
    result = bounded_execution_result(
        stdout="",
        stderr=stderr,
        exit_code=2,
        max_output_chars=64,
    )

    assert len(result.output) == 64
    assert result.stdout == ""
    assert result.stderr.endswith("fatal tail\n")
    assert "truncated" in result.stderr


def test_failed_long_streams_share_the_total_cap() -> None:
    result = bounded_execution_result(
        stdout="out" * 100,
        stderr="err" * 100,
        exit_code=2,
        max_output_chars=101,
    )

    assert len(result.output) == 101
    assert result.stdout.startswith("out")
    assert result.stdout.endswith("\n...[truncated]...\n")
    assert result.stderr == ("err" * 100)[-len(result.stderr) :]
    assert result.output == result.stdout + result.stderr


def test_character_cap_does_not_split_or_overcount_unicode() -> None:
    result = bounded_execution_result(
        stdout="🧪" * 100,
        stderr="致命的なエラー",
        exit_code=1,
        max_output_chars=50,
    )

    assert len(result.output) == 50
    assert result.stderr.endswith("致命的なエラー")
    assert "�" not in result.output


@pytest.mark.parametrize("max_output_chars", [0, -1, 1, 11])
def test_tiny_caps_are_respected(max_output_chars: int) -> None:
    result = bounded_execution_result(
        stdout="stdout",
        stderr="stderr",
        exit_code=1,
        max_output_chars=max_output_chars,
    )

    assert len(result.output) <= max(0, max_output_chars)
    assert result.truncated


def test_unknown_exit_code_uses_success_prefix_semantics() -> None:
    result = bounded_execution_result(
        stdout="s" * 100,
        stderr="fatal tail",
        exit_code=None,
        max_output_chars=50,
    )

    assert result.output.startswith("s")
    assert "fatal tail" not in result.output
