from unittest.mock import MagicMock

from vibe_serve.agents.progress import CandidateProgress, RoundProgress
from vibe_serve.context import _RunContext
from vibe_serve.schemas import JudgeResponse, Verdict


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(
        analysis="fallback",
        feedback="fallback-feedback",
        verdict=Verdict.FAIL,
    )


def _make_context(tmp_path):
    ctx = object.__new__(_RunContext)
    ctx._progress_stack = []
    ctx.workspace = tmp_path
    ctx.gpu_env = lambda: {}
    ctx.agent_runner = MagicMock()
    ctx.agent_runner.invoke.return_value = _judge_fallback()
    return ctx


def test_progress_rendering_is_loop_owned():
    assert RoundProgress(3, 24).label() == "Round 3/24"
    assert CandidateProgress(2, 8, 1, 4).label() == "Round 2/8 Cand 1/4"


def test_run_context_progress_scope_restores_previous(tmp_path):
    ctx = _make_context(tmp_path)
    outer = RoundProgress(1, 3)
    inner = CandidateProgress(2, 3, 1, 2)

    assert ctx.current_progress() is None
    with ctx.progress(outer):
        assert ctx.current_progress() is outer
        with ctx.progress(inner):
            assert ctx.current_progress() is inner
        assert ctx.current_progress() is outer
    assert ctx.current_progress() is None


def test_run_context_injects_current_progress(tmp_path):
    ctx = _make_context(tmp_path)
    progress = RoundProgress(2, 5)

    with ctx.progress(progress):
        ctx.invoke(
            kind="judge",
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
        )

    assert ctx.agent_runner.invoke.call_args.kwargs["progress"] is progress


def test_run_context_explicit_progress_overrides_scope(tmp_path):
    ctx = _make_context(tmp_path)
    scoped = RoundProgress(2, 5)
    explicit = CandidateProgress(2, 5, 1, 3)

    with ctx.progress(scoped):
        ctx.invoke(
            kind="judge",
            system_prompt="sys",
            user_prompt="usr",
            response_cls=JudgeResponse,
            fallback_factory=_judge_fallback,
            round_label="judge #1",
            progress=explicit,
        )

    assert ctx.agent_runner.invoke.call_args.kwargs["progress"] is explicit
