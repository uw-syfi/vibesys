"""Differential test: ``ctx.agents.turn`` renders the same prompt text for
every ``evolve`` role (mutator, judge, and the resolved profiler kind).

``vibesys.loops.evolve.loop`` no longer renders prompts itself (phase 4c):
every candidate turn goes through ``ctx.agents.turn(role, ...)`` with the
roles declared in ``roles/mutator.py`` and evolve's candidate roles in
``roles/judge.py``/``roles/profiler.py``. So the render call this test observes and
replays is the one ``vibesys.orchestration.agents`` issues, not one
``loop.py`` makes directly.

Method: run the real ``evolve`` strategy's bootstrap-pass scenario end-to-end
(the same scripted ``FakeAgentClient`` harness
``tests/vibesys/golden/test_evolve_golden.py`` uses), recording every
``render_template(name, template_dir=..., **kwargs)`` call
``vibesys.orchestration.agents`` makes alongside its rendered text. A
first-attempt bootstrap pass exercises exactly one turn of each of evolve's
three candidate roles. For each, replay ``ctx.agents.turn`` on a fresh test
context with the matching declared ``Role`` and the same captured context
kwargs, and assert its rendered system prompt is byte-identical to what the
real run actually sent the fake agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

from tests.vibesys.golden.test_evolve_golden import (
    _implementer,
    _judge,
    _mutator_writes_callback,
    _options,
    _profiler,
    _run_evolve,
)
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.loops.evolve.orchestration import descriptor_from_options
from vibesys.profilers import ProfilerKind
from vibesys.prompts.renderer import render_template as _real_render_template
from vibesys.roles.common import Verdict
from vibesys.roles.judge import CANDIDATE_JUDGE
from vibesys.roles.mutator import CANDIDATE_MUTATOR
from vibesys.roles.profiler import CANDIDATE_PROFILERS
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import Role


@dataclass(slots=True)
class _RecordedRender:
    name: str
    kwargs: dict[str, object] = field(default_factory=dict)
    text: str = ""


def _record_render_calls(recorded: list[_RecordedRender]):  # noqa: ANN202  # tracked: #288
    def _wrapper(name: str, *, template_dir=None, **kwargs: object) -> str:  # noqa: ANN001  # tracked: #288
        text = _real_render_template(name, template_dir=template_dir, **kwargs)
        recorded.append(_RecordedRender(name=name, kwargs=kwargs, text=text))
        return text

    return _wrapper


def _replay(tmp_path: Path, role: Role, context: dict[str, object], label: str) -> str:
    """Call ``ctx.agents.turn`` on a fresh context and return its system prompt."""
    replay_runner = FakeAgentClient(backend_name="stub")
    replay_runner.enqueue("testrole", role.fallback())

    async def body(ctx: RunContext) -> str:
        agent = await ctx.agents.spawn(ctx.agents.default_definition("testrole"))
        try:
            await ctx.agents.turn(role, agent=agent, context=context, label=label)
        finally:
            await agent.close()
        return replay_runner.calls_for("testrole")[0].system_prompt

    return run_with_context(tmp_path, replay_runner, body)


def test_every_evolve_role_prompt_matches_ctx_agents_turn(tmp_path: Path) -> None:
    recorded: list[_RecordedRender] = []
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    runner.enqueue("implementer", _implementer("bootstrap seed"))
    runner.enqueue("judge", _judge(Verdict.PASS))
    runner.enqueue("profiler", _profiler(10.0))

    descriptor = descriptor_from_options(
        _options(max_generations=1, children_per_generation=1, bootstrap_max_attempts=1)
    )
    with patch("vibesys.orchestration.agents.render_template", _record_render_calls(recorded)):
        real_run = _run_evolve(tmp_path / "real", descriptor=descriptor, runner=runner)
    assert real_run.result is True

    def recorded_for(name: str) -> _RecordedRender:
        return next(call for call in recorded if call.name == name)

    real_mutator_prompt = runner.calls_for("implementer")[0].system_prompt
    real_judge_prompt = runner.calls_for("judge")[0].system_prompt
    real_profiler_prompt = runner.calls_for("profiler")[0].system_prompt

    # The bootstrap-pass scenario resolves `ProfilerKind.AUTO` to one concrete
    # kind for this input bundle's domain; read which one back off the
    # recorded render rather than hardcoding it.
    profiler_template = next(call.name for call in recorded if call.name.startswith("profilers/"))
    profiler_kind = ProfilerKind(profiler_template.removeprefix("profilers/").removesuffix(".j2"))

    cases: tuple[tuple[Role, str, str, str], ...] = (
        (CANDIDATE_MUTATOR, "mutator_prompt.j2", real_mutator_prompt, "replay-mutator"),
        (CANDIDATE_JUDGE, "judge_prompt.j2", real_judge_prompt, "replay-judge"),
        (
            CANDIDATE_PROFILERS[profiler_kind],
            profiler_template,
            real_profiler_prompt,
            "replay-profiler",
        ),
    )
    for index, (role, template_name, real_prompt, label) in enumerate(cases):
        call = recorded_for(template_name)
        assert call.text == real_prompt, f"recorded render for {template_name} mismatched"
        replayed = _replay(tmp_path / f"replay-{index}", role, call.kwargs, label)
        assert replayed == real_prompt, f"ctx.agents.turn mismatched the real prompt for {role.id}"
