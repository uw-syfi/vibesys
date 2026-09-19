"""Focused tests for the profile-guided attribution process adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from vibesys.input_manifest import ProfileGuidedInput
from vibesys.loops.agent import profile_guidance as runner
from vibesys.loops.agent.model import ProfileBottleneck

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.protocol import LoopContext


class _Result:
    def __init__(self, exit_code: int, output: str) -> None:
        self.exit_code = exit_code
        self.output = output


class _Backend:
    def __init__(self, result: _Result | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, int | None]] = []

    def execute(self, command: str, timeout: int | None = None) -> _Result:
        self.calls.append((command, timeout))
        if len(self.calls) == 1:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return _Result(0, "")


class _Context:
    def __init__(self, workspace: Path, backend: _Backend) -> None:
        self.workspace = workspace
        self.judge_backend = backend
        self.logs: list[str] = []

    def lprint(self, message: str) -> None:
        self.logs.append(message)


def _config() -> ProfileGuidedInput:
    return ProfileGuidedInput(
        command=("custom-profiler", "--mode", "cpu"),
        timeout_seconds=73,
    )


def _framed(payload: str) -> str:
    return (
        f"diagnostic\n{runner._ATTRIBUTION_MARKER}\n{payload}\n"  # noqa: SLF001
        f"{runner._ATTRIBUTION_END_MARKER}\n"  # noqa: SLF001
    )


def test_run_attribution_uses_manifest_command_timeout_and_fixed_output_flag(
    tmp_path: Path,
) -> None:
    payload = (
        '{"version":1,"cost_unit":"instructions","components":['
        '{"name":"trace/implementations","cost":536,"share":0.536,'
        '"evidence":["merge_by"]}]}'
    )
    backend = _Backend(_Result(0, _framed(payload)))
    context = _Context(tmp_path, backend)

    result = runner.run_attribution(
        cast("LoopContext", context),
        _config(),
        round_number=2,
    )

    assert result == (
        ProfileBottleneck(
            name="trace/implementations",
            cost=536.0,
            share=0.536,
            evidence=["merge_by"],
        ),
    )
    command, timeout = backend.calls[0]
    assert "custom-profiler --mode cpu --vs-output /tmp/vibesys-attribution-2-" in command
    assert timeout == 73
    assert len(backend.calls) == 2
    assert backend.calls[1][0].startswith("rm -f -- /tmp/vibesys-attribution-2-")


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (_Result(2, "profiler failed"), "exit code 2"),
        (_Result(0, "no framed artifact"), "must write protocol v1 JSON"),
        (_Result(0, _framed("{broken")), "invalid result protocol v1"),
        (
            _Result(
                0,
                _framed(
                    '{"version":1,"cost_unit":"instructions","components":[],"unexpected":true}'
                ),
            ),
            "invalid result protocol v1",
        ),
        (
            _Result(
                0,
                _framed(
                    '{"version":1,"cost_unit":"instructions","components":['
                    '{"name":"x","cost":1,"share":0.5,"evidence":[],"pct":50}]}'
                ),
            ),
            "invalid result protocol v1",
        ),
    ],
)
def test_run_attribution_raises_for_execution_or_exact_protocol_failure(
    tmp_path: Path,
    result: _Result,
    match: str,
) -> None:
    context = _Context(tmp_path, _Backend(result))
    with pytest.raises(RuntimeError, match=match):
        runner.run_attribution(cast("LoopContext", context), _config(), round_number=1)


def test_run_attribution_wraps_backend_failure_and_still_cleans_up(tmp_path: Path) -> None:
    backend = _Backend(OSError("backend unavailable"))
    context = _Context(tmp_path, backend)

    with pytest.raises(RuntimeError, match="could not be executed: backend unavailable"):
        runner.run_attribution(cast("LoopContext", context), _config(), round_number=1)

    assert len(backend.calls) == 2
