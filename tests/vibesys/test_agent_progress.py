from pathlib import Path

import pytest

from vibesys.context import _RunContext
from vibesys.run import RunPaths
from vibesys.run.integration import LocalRunIntegration
from vibesys.schemas import JudgeResponse, Verdict
from vs_agent.api import CandidateProgress, RoundProgress
from vs_agent.api.testing import FakeAgentClient


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(
        analysis="fallback",
        feedback="fallback-feedback",
        verdict=Verdict.FAIL,
    )


def _make_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> tuple[_RunContext, FakeAgentClient]:
    ctx = object.__new__(_RunContext)
    ctx.integration = LocalRunIntegration()
    request.addfinalizer(ctx.integration.close)
    ctx.events = ctx.integration.events
    # lint-waiver: LW-010043 [SLF001]; this minimal fixture bypasses the resource-owning constructor to exercise public progress scoping without provisioning a project.
    ctx._progress_stack = []  # noqa: SLF001
    # lint-waiver: LW-010044 [SLF001]; public invoke() requires canonical log paths, and constructing a full run context would add unrelated project resources to this unit test.
    ctx._paths = RunPaths(  # noqa: SLF001
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        run_log_path=tmp_path / "run.log",
    )
    monkeypatch.setattr(ctx, "gpu_env", dict)
    # Every invocation event carries the client's attribution, so the fake
    # supplies real strings the event payload can validate.
    client = FakeAgentClient(driver_name="mock", provider="mock", model="mock-model")
    client.set_response("judge", _judge_fallback())
    ctx.agent_client = client
    return ctx, client


def test_progress_rendering_is_loop_owned() -> None:
    assert RoundProgress(3, 24).label() == "Round 3/24"
    assert CandidateProgress(2, 8, 1, 4).label() == "Round 2/8 Cand 1/4"


def test_run_context_progress_scope_restores_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    ctx, _client = _make_context(tmp_path, monkeypatch, request)
    outer = RoundProgress(1, 3)
    inner = CandidateProgress(2, 3, 1, 2)

    assert ctx.current_progress() is None
    with ctx.progress(outer):
        assert ctx.current_progress() is outer
        with ctx.progress(inner):
            assert ctx.current_progress() is inner
        assert ctx.current_progress() is outer
    assert ctx.current_progress() is None


def test_run_context_injects_current_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    ctx, client = _make_context(tmp_path, monkeypatch, request)
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

    assert client.calls_for("judge")[0].progress is progress


def test_run_context_explicit_progress_overrides_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    ctx, client = _make_context(tmp_path, monkeypatch, request)
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

    assert client.calls_for("judge")[0].progress is explicit
