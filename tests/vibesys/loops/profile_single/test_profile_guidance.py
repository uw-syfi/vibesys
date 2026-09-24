"""Focused tests for the profile-guided attribution process adapter."""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.agent_run.state import ProfileBottleneck
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.loops.profile_single import attribution as runner

if TYPE_CHECKING:
    from vibesys.orchestration.runtime import RunContext


class _Result:
    def __init__(self, exit_code: int, output: str) -> None:
        self.exit_code = exit_code
        self.output = output


class _Backend:
    def __init__(self, result: _Result | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, int | None]] = []

    async def execute(self, command: str, *, timeout_seconds: int | None = None) -> _Result:
        self.calls.append((command, timeout_seconds))
        if len(self.calls) == 1:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return _Result(0, "")


class _Context:
    def __init__(self, backend: _Backend) -> None:
        self.environment = backend
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)


def _config() -> ProfileGuidedInput:
    return ProfileGuidedInput(
        command=("custom-profiler", "--mode", "cpu"),
        timeout_seconds=73,
    )


def _framed(payload: str) -> str:
    return (
        f"diagnostic\n{runner._BEGIN}\n{payload}\n"  # noqa: SLF001
        f"{runner._END}\n"  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_run_attribution_uses_manifest_command_timeout_and_fixed_output_flag() -> None:
    payload = (
        '{"version":1,"cost_unit":"instructions","components":['
        '{"name":"trace/implementations","cost":536,"share":0.536,'
        '"evidence":["merge_by"]}]}'
    )
    backend = _Backend(_Result(0, _framed(payload)))
    context = _Context(backend)

    result = await runner.run_attribution(
        cast("RunContext", context),
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
    assert (
        f"custom-profiler --mode cpu --vs-output {tempfile.gettempdir()}/vibesys-attribution-2-"
        in command
    )
    assert timeout == 73
    assert len(backend.calls) == 2
    assert backend.calls[1][0].startswith(
        f"rm -f -- {tempfile.gettempdir()}/vibesys-attribution-2-"
    )


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
@pytest.mark.asyncio
async def test_run_attribution_raises_for_execution_or_exact_protocol_failure(
    result: _Result, match: str
) -> None:
    context = _Context(_Backend(result))
    with pytest.raises(RuntimeError, match=match):
        await runner.run_attribution(cast("RunContext", context), _config(), round_number=1)


@pytest.mark.asyncio
async def test_run_attribution_wraps_backend_failure_and_still_cleans_up() -> None:
    backend = _Backend(OSError("backend unavailable"))
    context = _Context(backend)

    with pytest.raises(RuntimeError, match="could not be executed: backend unavailable"):
        await runner.run_attribution(cast("RunContext", context), _config(), round_number=1)

    assert len(backend.calls) == 2
